FROM python:3.12-slim

RUN pip install --no-cache-dir pillow

WORKDIR /app

COPY app.py index.html config.json ./

EXPOSE 7860

CMD ["python", "app.py"]
