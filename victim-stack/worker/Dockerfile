FROM python:3.11-slim

WORKDIR /app
COPY worker/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common/obs.py /app/obs.py
COPY worker/main.py /app/main.py

EXPOSE 9100
# Unbuffered so the last lines before an OOM kill actually reach the log file.
CMD ["python", "-u", "main.py"]
