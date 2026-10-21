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

const baseURL = __ENV.BASE_URL;

export default function () {
  if (__ENV.WORKLOAD === "info") {
    const response = http.get(`${baseURL}/info`);
    check(response, {
      "status is 200": (r) => r.status === 200,
      "has response id": (r) => Boolean(r.headers["X-Response-Id"]),
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
      "has authenticated user": (r) => Boolean(r.json("user.id")),
    });
  } else if (__ENV.WORKLOAD === "catalog") {
    const response = http.get(`${baseURL}/catalog`);
    check(response, {
      "status is 200": (r) => r.status === 200,
      "has product id": (r) => r.json("id") === "prod-1",
      "has vendor": (r) => Boolean(r.json("vendor.name")),
    });
  } else if (__ENV.WORKLOAD === "profile") {
    const response = http.get(`${baseURL}/users/me`, {
      headers: { Authorization: `Bearer ${__ENV.TOKEN}` },
    });
    check(response, {
      "status is 200": (r) => r.status === 200,
      "id from token": (r) => r.json("id") === "user-1",
      "has city": (r) => Boolean(r.json("city")),
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
