FROM oven/bun:1

WORKDIR /app

COPY package.json bun.lock bunfig.toml ./
COPY packages/ ./packages/
COPY apps/ ./apps/

RUN bun install

# Download the Pi binary for OpenAI support
RUN apt-get update && apt-get install -y curl && \
    mkdir -p /app/bin && \
    curl -fsSL https://agents.craft.do/pi/linux-x64 -o /app/bin/pi && \
    chmod +x /app/bin/pi

ENV PI_SERVER_PATH=/app/bin/pi

EXPOSE 9100

CMD ["bun", "run", "packages/server/src/index.ts", "--allow-insecure-bind"]
