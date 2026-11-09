FROM python:3.11-slim
WORKDIR /app

COPY academic-kb-prototype-MVP/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY academic-kb-prototype-MVP/ ./

EXPOSE 80
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80"]