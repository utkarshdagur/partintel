# Use a lightweight Python base image
FROM python:3.11-slim

# Set the working directory
WORKDIR /app

# Copy the requirements file and install dependencies
# We use --no-cache-dir to keep the image small
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the rest of the application code
COPY . .

# Expose the default port the app runs on
EXPOSE 8765

# Set environment variables
ENV HOST=0.0.0.0
ENV PORT=8765

# Start the ingestion server, binding to 0.0.0.0 so it's accessible outside the container
CMD ["sh", "-c", "python run_ingest.py --host $HOST --port $PORT"]
