FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code source du service de detection. Le correlator lit les ground-truth
# depuis scenario-runner/results (monte via volume dans docker-compose).
COPY *.py ./

# Commande surchargee par docker-compose selon le role
# (scraper / detector / baseline). Defaut : le detecteur.
CMD ["python", "-u", "detector.py"]
