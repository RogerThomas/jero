group "default" {
  targets = ["app", "runner", "upstream"]
}

target "_common" {
  context = "."
  dockerfile = "benchmarks/ci/Dockerfile"
}

target "app" {
  inherits = ["_common"]
  target = "app"
  tags = ["jero-benchmark-app:local"]
}

target "runner" {
  inherits = ["_common"]
  target = "runner"
  tags = ["jero-benchmark-runner:local"]
}

target "upstream" {
  context = "benchmarks/ci/upstream"
  dockerfile = "Dockerfile"
  tags = ["jero-benchmark-upstream:local"]
}
