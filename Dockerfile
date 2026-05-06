FROM python:3.12-alpine
RUN apk add --no-cache curl
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY *.txt .
COPY *.txt.meta .
CMD ["python", "main.py"]
