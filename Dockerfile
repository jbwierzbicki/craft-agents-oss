FROM ghcr.io/lukilabs/craft-agents-server:latest

USER root
ENV HOME=/home/craftagents

RUN apt-get update && apt-get install -y openssl && apt-get clean && \
    mkdir -p /certs && \
    openssl req -x509 -newkey rsa:2048 \
    -keyout /certs/key.pem \
    -out /certs/cert.pem \
    -days 3650 -nodes \
    -subj "/CN=craft-agent"

ENV CRAFT_RPC_TLS_CERT=/certs/cert.pem
ENV CRAFT_RPC_TLS_KEY=/certs/key.pem

EXPOSE 9100
