FROM rust:1-alpine AS build
RUN apk add --no-cache musl-dev
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY src ./src
RUN cargo build --release --locked

FROM alpine:3.23
COPY --from=build /src/target/release/jero-benchmark-upstream /bin/upstream
ENTRYPOINT ["/bin/upstream"]
