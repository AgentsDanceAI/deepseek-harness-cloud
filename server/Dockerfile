FROM python:3.11-slim

WORKDIR /srv/dhc

# Install only the dependencies (declared in pyproject), then run from the copied
# source tree via PYTHONPATH. We deliberately do NOT `pip install .` the package:
# templates/, static/, and config/ are resolved through __file__-relative paths at
# runtime, and a site-packages install would drop those non-.py data files.
COPY server/pyproject.toml ./
RUN pip install --no-cache-dir \
        "fastapi>=0.115" "uvicorn[standard]>=0.30" "httpx>=0.27" "jinja2>=3.1" \
        "python-multipart>=0.0.9" "itsdangerous>=2.2" "cryptography>=42"

COPY server/app ./app
COPY server/config ./config
# The legal documents live at the repo root and are read at request time by
# webpages._legal_dir(). Without this they are absent from the image and every
# /legal/* page renders empty — which is exactly what a payment processor's
# review would see. The build context is the repo root so they can be copied.
COPY legal /srv/legal

ENV PYTHONPATH=/srv/dhc
ENV DHC_DATA_DIR=/app/data
VOLUME /app/data

EXPOSE 8100
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8100"]
