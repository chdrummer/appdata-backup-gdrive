FROM python:3.12-alpine
RUN apk add --no-cache rclone docker-cli bash tar zstd tzdata \
 && pip install --no-cache-dir flask apscheduler
WORKDIR /app
COPY app/ /app/
ENV CONFIG_DIR=/config SOURCE_DIR=/source TZ=America/New_York SELF_NAME=appdata-backup-gdrive
EXPOSE 8080
VOLUME /config
CMD ["python", "-u", "/app/main.py"]
