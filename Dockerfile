FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# the script may have Windows line endings when built from a Windows checkout
RUN sed -i 's/\r$//' docker-entrypoint.sh
EXPOSE 8000
CMD ["sh", "/app/docker-entrypoint.sh"]
