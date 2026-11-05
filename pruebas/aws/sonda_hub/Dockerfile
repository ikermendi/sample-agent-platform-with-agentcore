FROM public.ecr.aws/docker/library/python:3.12-slim
RUN pip install --no-cache-dir strands-agents==1.63.0 mcp==2.3.0 starlette==1.10.0 uvicorn==0.54.0 httpx==0.28.1 boto3 cryptography
COPY *.py /app/
WORKDIR /app
EXPOSE 8080
CMD ["python", "kernel.py"]
