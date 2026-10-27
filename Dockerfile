FROM python:3.10-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    pulseaudio \
    scrot \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create required directories
RUN mkdir -p logs downloads screenshots temp

# Run validation and start
CMD python validate_startup.py && python main.py