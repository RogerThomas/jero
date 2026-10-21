//! A static, API-key-protected JSON upstream, matching the AWS workload.
use axum::{
    extract::State,
    http::{header, HeaderMap, StatusCode},
    response::{IntoResponse, Response},
    routing::get,
    Router,
};

const PAYLOAD: &str = r#"{"id":"prod-1","title":"Mechanical Keyboard","price":129.99,"inStock":true,"tags":["electronics","peripherals","keyboard"],"rating":4.7,"description":"A tactile mechanical keyboard with hot-swappable switches.","vendor":{"id":"vendor-9","name":"KeyCo"}}"#;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let key = std::env::var("UPSTREAM_API_KEY")?;
    let app = Router::new()
        .route("/data", get(data))
        .route("/health", get(|| async { "ok" }))
        .with_state(key);
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8001").await?;
    axum::serve(listener, app).await?;
    Ok(())
}

async fn data(State(key): State<String>, headers: HeaderMap) -> Response {
    let token = headers
        .get(header::AUTHORIZATION)
        .and_then(|v| v.to_str().ok())
        .and_then(|v| v.strip_prefix("Bearer "))
        .map(str::trim);
    if token != Some(key.as_str()) {
        return StatusCode::UNAUTHORIZED.into_response();
    }
    ([(header::CONTENT_TYPE, "application/json")], PAYLOAD).into_response()
}
