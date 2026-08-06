FROM python:3.11-slim

WORKDIR /app
COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY common/obs.py /app/obs.py
COPY api/db.py api/main.py /app/

EXPOSE 8000
# --no-access-log: uvicorn's access log would pollute the contract log file.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
