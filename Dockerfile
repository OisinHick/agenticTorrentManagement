FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the agent code and static assets
COPY agent.py .
COPY static/ ./static/

# Expose port
EXPOSE 4343

# Run the agent
CMD ["python", "-u", "agent.py"]
