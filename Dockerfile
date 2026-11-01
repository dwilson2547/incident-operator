# incident-operator: webhook (8081), UI (8080) and the triage loop in one process.
# Dependencies before code, so a code change rebuilds only the top layer. No cluster specifics in the image: the
# environment primer (and later the runbooks) come from the chart as a ConfigMap.
FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
RUN useradd --uid 10001 --create-home app
WORKDIR /app
COPY pyproject.toml ./
RUN python -c "import tomllib; open('/tmp/req.txt', 'w').write('\\n'.join(tomllib.load(open('pyproject.toml', 'rb'))['project']['dependencies']))" \
 && pip install -r /tmp/req.txt
COPY src ./src
RUN pip install --no-deps . \
 && python -c "import incident_operator.__main__; from importlib.resources import files; assert 'Incidents' in files('incident_operator').joinpath('static/index.html').read_text()"
USER app
EXPOSE 8080 8081
CMD ["python", "-m", "incident_operator"]
