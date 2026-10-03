FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY cleanup_plan.py delete_watched_episodes.py sonarr_client.py ./
ENTRYPOINT ["python", "delete_watched_episodes.py"]
