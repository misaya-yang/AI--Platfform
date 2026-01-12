# =============================================================================
# AI Gateway Frontend - Multi-stage Dockerfile
# =============================================================================
# Build: docker build -t ai-gateway-web:latest .
# Run:   docker run -p 80:80 ai-gateway-web:latest
# =============================================================================

# -----------------------------------------------------------------------------
# Stage 1: Builder - Build the React application
# -----------------------------------------------------------------------------
FROM node:20-alpine AS builder

WORKDIR /app

# Install pnpm
RUN corepack enable && corepack prepare pnpm@10.27.0 --activate

# Copy package files
COPY package.json pnpm-lock.yaml ./

# Install dependencies
RUN pnpm install --frozen-lockfile

# Copy source code
COPY . .

# Build arguments for environment variables
ARG VITE_API_URL=
ARG VITE_API_BASE_URL=

# Set environment variables for build
ENV VITE_API_URL=${VITE_API_URL}
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL}

# Build the application
RUN pnpm build

# -----------------------------------------------------------------------------
# Stage 2: Runtime - Nginx serving static files
# -----------------------------------------------------------------------------
FROM nginx:alpine AS runtime

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy built assets from builder
COPY --from=builder /app/dist /usr/share/nginx/html

# Expose port
EXPOSE 80

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
    CMD wget --no-verbose --tries=1 --spider http://localhost:80/health || exit 1

# Run nginx
CMD ["nginx", "-g", "daemon off;"]
