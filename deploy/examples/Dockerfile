# Supply an operator-reviewed, native Python image pinned by immutable digest.
ARG PYTHON_BASE
FROM ${PYTHON_BASE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_INPUT=1
WORKDIR /tmp
USER 65534:65534
CMD ["python", "--version"]
