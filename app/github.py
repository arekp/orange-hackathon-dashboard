"""Klient GitHub API (GraphQL + REST) oparty wyłącznie na bibliotece standardowej."""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request

log = logging.getLogger("github")

API = "https://api.github.com"


class GitHubError(RuntimeError):
    pass


class GitHubClient:
    def __init__(self, token: str, user_agent: str = "hackathon-dashboard"):
        if not token:
            raise GitHubError("Brak GITHUB_TOKEN. Uzupełnij plik .env albo uruchom tryb demo (--demo).")
        self.token = token
        self.user_agent = user_agent
        self.rate_remaining: int | None = None
        self.rate_reset: int | None = None

    # --- transport ---

    def _request(self, method: str, url: str, body: dict | None = None, retries: int = 4) -> dict | list:
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": self.user_agent,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        delay = 2.0
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    self._track_rate(resp.headers)
                    return json.loads(resp.read().decode() or "null")
            except urllib.error.HTTPError as e:
                self._track_rate(e.headers)
                payload = e.read().decode(errors="replace")
                retry_after = e.headers.get("Retry-After") if e.headers else None
                # Limit drugorzędny / chwilowe błędy serwera -> ponów z opóźnieniem
                if e.code in (502, 503, 504) or (e.code in (403, 429) and (retry_after or "rate limit" in payload.lower())):
                    if attempt < retries:
                        wait = float(retry_after) if retry_after else delay
                        log.warning("GitHub %s, ponawiam za %.0fs", e.code, wait)
                        time.sleep(min(wait, 120))
                        delay *= 2
                        continue
                raise GitHubError(f"HTTP {e.code} dla {url}: {payload[:300]}") from e
            except urllib.error.URLError as e:
                if attempt < retries:
                    log.warning("Błąd sieci (%s), ponawiam za %.0fs", e.reason, delay)
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise GitHubError(f"Błąd sieci: {e.reason}") from e
        raise GitHubError("Nie udało się wykonać zapytania")

    def _track_rate(self, headers) -> None:
        if not headers:
            return
        remaining = headers.get("x-ratelimit-remaining")
        reset = headers.get("x-ratelimit-reset")
        if remaining is not None:
            self.rate_remaining = int(remaining)
        if reset is not None:
            self.rate_reset = int(reset)

    def throttle_if_needed(self, floor: int = 200) -> None:
        """Gdy kończy się limit, czekamy do jego odnowienia zamiast dostawać błędy."""
        if self.rate_remaining is not None and self.rate_remaining < floor and self.rate_reset:
            wait = max(0, self.rate_reset - int(time.time())) + 5
            log.warning("Zostało %s zapytań w limicie, czekam %ss", self.rate_remaining, wait)
            time.sleep(min(wait, 900))

    def graphql(self, query: str, variables: dict) -> dict:
        result = self._request("POST", f"{API}/graphql", {"query": query, "variables": variables})
        if result.get("errors"):
            raise GitHubError(f"GraphQL: {json.dumps(result['errors'])[:500]}")
        return result["data"]

    def rest(self, path: str) -> dict | list:
        return self._request("GET", f"{API}{path}")

    # --- zapytania domenowe ---

    def list_org_repos(self, org: str) -> list[dict]:
        q = """
        query($org: String!, $after: String) {
          organization(login: $org) {
            repositories(first: 100, after: $after, orderBy: {field: CREATED_AT, direction: ASC}) {
              pageInfo { hasNextPage endCursor }
              nodes {
                name isArchived
                defaultBranchRef { name }
                repositoryTopics(first: 20) { nodes { topic { name } } }
              }
            }
          }
        }"""
        repos, after = [], None
        while True:
            data = self.graphql(q, {"org": org, "after": after})
            org_data = data.get("organization")
            if org_data is None:
                raise GitHubError(f"Organizacja {org} nie istnieje albo token nie ma do niej dostępu")
            page = org_data["repositories"]
            for n in page["nodes"]:
                repos.append({
                    "name": n["name"],
                    "archived": n["isArchived"],
                    "default_branch": (n.get("defaultBranchRef") or {}).get("name"),
                    "topics": [t["topic"]["name"] for t in n["repositoryTopics"]["nodes"]],
                })
            if not page["pageInfo"]["hasNextPage"]:
                return repos
            after = page["pageInfo"]["endCursor"]

    def list_branches(self, owner: str, repo: str) -> list[tuple[str, str]]:
        q = """
        query($owner: String!, $repo: String!, $after: String) {
          repository(owner: $owner, name: $repo) {
            refs(refPrefix: "refs/heads/", first: 100, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes { name target { oid } }
            }
          }
        }"""
        out, after = [], None
        while True:
            data = self.graphql(q, {"owner": owner, "repo": repo, "after": after})
            refs = data["repository"]["refs"]
            out += [(n["name"], n["target"]["oid"]) for n in refs["nodes"] if n.get("target")]
            if not refs["pageInfo"]["hasNextPage"]:
                return out
            after = refs["pageInfo"]["endCursor"]

    def branch_history(self, owner: str, repo: str, branch: str, since_iso: str) -> list[dict]:
        q = """
        query($owner: String!, $repo: String!, $ref: String!, $since: GitTimestamp!, $after: String) {
          repository(owner: $owner, name: $repo) {
            ref(qualifiedName: $ref) {
              target {
                ... on Commit {
                  history(first: 100, since: $since, after: $after) {
                    pageInfo { hasNextPage endCursor }
                    nodes {
                      oid committedDate additions deletions changedFilesIfAvailable
                      parents { totalCount }
                      author { name email user { login } }
                    }
                  }
                }
              }
            }
          }
        }"""
        out, after = [], None
        while True:
            data = self.graphql(q, {"owner": owner, "repo": repo, "ref": f"refs/heads/{branch}",
                                    "since": since_iso, "after": after})
            ref = data["repository"]["ref"]
            if not ref or not ref.get("target") or "history" not in ref["target"]:
                return out
            hist = ref["target"]["history"]
            out += hist["nodes"]
            if not hist["pageInfo"]["hasNextPage"]:
                return out
            after = hist["pageInfo"]["endCursor"]

    def commit_files(self, owner: str, repo: str, sha: str) -> list[dict]:
        data = self.rest(f"/repos/{owner}/{repo}/commits/{sha}")
        return data.get("files", []) if isinstance(data, dict) else []

    def pull_requests(self, owner: str, repo: str, updated_since_iso: str | None) -> list[dict]:
        q = """
        query($owner: String!, $repo: String!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequests(first: 50, after: $after, orderBy: {field: UPDATED_AT, direction: DESC}) {
              pageInfo { hasNextPage endCursor }
              nodes { number title state createdAt mergedAt closedAt updatedAt additions deletions }
            }
          }
        }"""
        out, after = [], None
        while True:
            data = self.graphql(q, {"owner": owner, "repo": repo, "after": after})
            prs = data["repository"]["pullRequests"]
            for n in prs["nodes"]:
                if updated_since_iso and n["updatedAt"] < updated_since_iso:
                    return out
                out.append(n)
            if not prs["pageInfo"]["hasNextPage"]:
                return out
            after = prs["pageInfo"]["endCursor"]
