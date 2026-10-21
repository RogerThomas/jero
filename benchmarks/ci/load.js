import http from "k6/http";
import { check } from "k6";

export const options = {
  vus: Number(__ENV.VUS),
  duration: `${__ENV.DURATION}s`,
  summaryTrendStats: ["avg", "p(99)"],
  thresholds: {
    http_req_failed: ["rate==0"],
    checks: ["rate==1"],
    http_reqs: ["count>0"],
  },
};

const baseURL = "http://127.0.0.1:8000";
const movie = JSON.parse(__ENV.MOVIE);
const product = JSON.parse(__ENV.PRODUCT);
const profile = JSON.parse(__ENV.PROFILE);

function sameJSON(actual, expected) {
  if (expected !== null && typeof expected === "object") {
    return actual !== null && typeof actual === "object"
      && Array.isArray(actual) === Array.isArray(expected)
      && Object.keys(actual).length === Object.keys(expected).length
      && Object.keys(expected).every((key) => sameJSON(actual[key], expected[key]));
  }
  return actual === expected;
}

export default function () {
  if (__ENV.WORKLOAD === "info") {
    const response = http.get(`${baseURL}/info`);
    check(response, {
      "status is 200": (r) => r.status === 200,
      "has response id": (r) => Boolean(r.headers["X-Response-Id"]),
      "has expected payload": (r) => r.json("id") === "info-1" && r.json("nested.level") === 2,
    });
  } else if (__ENV.WORKLOAD === "movies") {
    const response = http.post(`${baseURL}/movies`, __ENV.MOVIE, {
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${__ENV.TOKEN}`,
      },
    });
    check(response, {
      "status is 201": (r) => r.status === 201,
      "has movie id": (r) => Boolean(r.json("id")),
      "has authenticated user": (r) => r.json("user.id") === "user-1",
      "echoes input": (r) => Object.keys(movie).every((key) => r.json(key) === movie[key]),
    });
  } else if (__ENV.WORKLOAD === "catalog" || __ENV.WORKLOAD === "profile") {
    const isProfile = __ENV.WORKLOAD === "profile";
    const response = http.get(`${baseURL}${isProfile ? "/users/me" : "/catalog"}`, {
      headers: isProfile ? { Authorization: `Bearer ${__ENV.TOKEN}` } : {},
    });
    check(response, {
      "status is 200": (r) => r.status === 200,
      "has complete expected payload": (r) => sameJSON(r.json(), isProfile ? profile : product),
    });
  } else {
    throw new Error(`Unknown workload: ${__ENV.WORKLOAD}`);
  }
}

export function handleSummary(data) {
  return {
    [__ENV.RESULT_FILE]: JSON.stringify({
      requests_per_second: data.metrics.http_reqs.values.rate,
      requests: data.metrics.http_reqs.values.count,
      mean_ms: data.metrics.http_req_duration.values.avg,
      p99_ms: data.metrics.http_req_duration.values["p(99)"],
      failed_rate: data.metrics.http_req_failed.values.rate,
      checks_rate: data.metrics.checks.values.rate,
      raw: data,
    }, null, 2),
  };
}
