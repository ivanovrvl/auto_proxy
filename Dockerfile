FROM python:3.12-alpine
WORKDIR /code
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py .
COPY *.txt .
CMD ["python", "main.py"]
