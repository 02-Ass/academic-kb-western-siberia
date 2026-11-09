FROM python:3.11-slim
WORKDIR /app

COPY academic-kb-prototype-MVP/backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY academic-kb-prototype-MVP/backend/ ./

EXPOSE 80
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "80"]