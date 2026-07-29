FROM python:3.11-alpine

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .

VOLUME ["/app/data"]

EXPOSE 5000

CMD ["python", "app.py"]
