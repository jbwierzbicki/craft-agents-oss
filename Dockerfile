FROM oven/bun:1

WORKDIR /app

COPY package.json bun.lock bunfig.toml ./
COPY packages/ ./packages/
COPY apps/ ./apps/

RUN bun install

EXPOSE 9100

CMD ["bun", "run", "packages/server/src/index.ts", "--allow-insecure-bind"]
