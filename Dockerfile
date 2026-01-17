FROM python:3.12.0-slim
WORKDIR /app
COPY . .
RUN pip install --no-cache-dir -r requirements.txt
CMD [ "python", "delete_watched_episodes.py" ]