FROM python:3.12-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1 ILO_DATA_DIR=/data
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY web ./web
VOLUME /data
EXPOSE 8080
CMD ["python", "-m", "app"]
