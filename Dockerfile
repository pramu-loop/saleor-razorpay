# Dockerfile for Razorpay App

# 1. Use a modern, slim Python runtime as a base image
FROM python:3.11-slim

# 2. Set the working directory inside the container
WORKDIR /usr/src/app

# 3. Install system-level build dependencies needed for the PostgreSQL adapter
RUN apt-get update && apt-get install -y --no-install-recommends gcc libpq-dev && rm -rf /var/lib/apt/lists/*

# 4. Copy the requirements file first, to leverage Docker's build cache
COPY requirements.txt ./

# 5. Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# 6. Copy the rest of your application's code
COPY . .

# 7. Expose the port the app runs on and define the startup command
EXPOSE 8000
CMD ["uvicorn", "main:main.app", "--host", "0.0.0.0", "--port", "8000"]
