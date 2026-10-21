FROM python:3.12-slim

WORKDIR /app

COPY app.py index.html config.json ./

EXPOSE 7860

CMD ["python", "app.py"]
