FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
ENV DATA_DIR=/data
RUN mkdir -p /data
EXPOSE 5000
CMD ["python", "app.py"]
