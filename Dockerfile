FROM python:3.12-slim
WORKDIR /app
COPY app ./app
COPY static ./static
COPY main.py ./
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
ENTRYPOINT ["python", "main.py"]
