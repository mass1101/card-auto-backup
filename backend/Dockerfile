FROM python:3.11-alpine
WORKDIR /app
COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
ENV DATA_DIR=/data
RUN mkdir -p /data
EXPOSE 5000
CMD ["python", "app.py"]
