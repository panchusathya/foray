FROM node:22-alpine

WORKDIR /app

COPY package.json ./
# No runtime dependencies by design — this stays a single copy step.
RUN npm install --omit=dev --no-audit --no-fund

COPY src ./src
COPY test ./test

USER node
EXPOSE 8787

CMD ["node", "src/index.js", "serve"]
