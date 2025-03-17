FROM python:3.12-slim

# Install avahi-daemon for mDNS resolution and ffmpeg
RUN apt-get update && apt-get install -y \
    ffmpeg && \
    rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy the requirements file
COPY requirements.txt .

# Install the dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application code
COPY asm.py .

# Set the environment variables (optional, can be overridden in docker-compose)
ENV PYTHONUNBUFFERED=1

# Command to run the application
CMD ["python", "asm.py"]