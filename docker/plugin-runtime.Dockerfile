FROM python:3.12-slim

ARG MYSQL_CONNECTOR_VERSION=26.7.0

RUN pip install --no-cache-dir \
        "mysql-connector-python==${MYSQL_CONNECTOR_VERSION}" \
        "pydantic>=2.10,<3" \
    && useradd --create-home --uid 65532 plugin

USER plugin
WORKDIR /plugin
