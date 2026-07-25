import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/index.js";

const ENV = {
  ALLOWED_TENANTS: "arlingtontx,pflugerville,fortworthgov,dentoncounty",
  PROXY_TOKEN: "test-secret",
};
const VALID_URL =
  "https://proxy.example/v1/archive/fortworthgov/" +
  "fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4";

function request(url = VALID_URL, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("authorization", "Bearer test-secret");
  return new Request(url, { ...options, headers });
}

test("requires bearer authentication before fetching upstream", async () => {
  let fetched = false;
  const response = await handleRequest(
    new Request(VALID_URL),
    ENV,
    async () => {
      fetched = true;
      return new Response();
    },
  );
  assert.equal(response.status, 401);
  assert.equal(response.headers.get("www-authenticate"), "Bearer");
  assert.equal(fetched, false);
});

test("rejects a present-but-incorrect bearer token", async () => {
  let fetched = false;
  const response = await handleRequest(
    new Request(VALID_URL, {
      headers: { authorization: "Bearer wrong-secret" },
    }),
    ENV,
    async () => {
      fetched = true;
      return new Response();
    },
  );
  assert.equal(response.status, 401);
  assert.equal(response.headers.get("www-authenticate"), "Bearer");
  assert.equal(fetched, false);
});

test("rejects arbitrary tenants, filenames, queries, and methods", async () => {
  const urls = [
    VALID_URL.replace("fortworthgov/", "unknown/"),
    VALID_URL.replace("fortworthgov_e4cc", "other_e4cc"),
    VALID_URL.replace(".mp4", "../secret.mp4"),
    `${VALID_URL}?url=https://example.com`,
  ];
  for (const url of urls) {
    const response = await handleRequest(request(url), ENV, async () => {
      throw new Error("must not fetch");
    });
    assert.equal(response.status, 404);
  }
  const post = await handleRequest(request(VALID_URL, { method: "POST" }), ENV);
  assert.equal(post.status, 405);
  assert.equal(post.headers.get("allow"), "GET, HEAD");
});

test("streams an allowed range and forwards only selected headers", async () => {
  let captured;
  const response = await handleRequest(
    request(VALID_URL, {
      headers: {
        range: "bytes=0-16777215",
        cookie: "must-not-forward",
        "x-untrusted": "must-not-forward",
      },
    }),
    ENV,
    async (url, init) => {
      captured = { url, init };
      return new Response("media-bytes", {
        status: 206,
        headers: {
          "accept-ranges": "bytes",
          "content-range": "bytes 0-10/100",
          "content-type": "video/mp4",
          "set-cookie": "must-not-return",
          "x-upstream-secret": "must-not-return",
        },
      });
    },
  );

  assert.equal(
    captured.url,
    "https://archive-video.granicus.com/fortworthgov/" +
      "fortworthgov_e4cc067f-6b2d-11f1-9494-005056a89546.mp4",
  );
  assert.equal(captured.init.redirect, "manual");
  assert.equal(captured.init.headers.get("range"), "bytes=0-16777215");
  assert.equal(captured.init.headers.get("authorization"), null);
  assert.equal(captured.init.headers.get("cookie"), null);
  assert.equal(captured.init.headers.get("x-untrusted"), null);
  assert.equal(response.status, 206);
  assert.equal(response.headers.get("content-range"), "bytes 0-10/100");
  assert.equal(response.headers.get("content-type"), "video/mp4");
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(response.headers.get("x-upstream-secret"), null);
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(await response.text(), "media-bytes");
});

test("plain GET without a Range header returns the full body", async () => {
  // CR2-WK-02: none of the existing tests exercise an unranged GET — a regression that broke
  // this specific (no-Range) path would otherwise slip past the suite.
  let captured;
  const response = await handleRequest(request(VALID_URL), ENV, async (url, init) => {
    captured = { url, init };
    return new Response("full-media-bytes", {
      status: 200,
      headers: {
        "content-length": "16",
        "content-type": "video/mp4",
      },
    });
  });

  assert.equal(captured.init.headers.get("range"), null);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-length"), "16");
  assert.equal(response.headers.get("content-type"), "video/mp4");
  assert.equal(await response.text(), "full-media-bytes");
});

test("proxies allow-listed Granicus metadata and preserves DownloadFile redirects", async () => {
  const metadata =
    "https://proxy.example/v1/granicus/arlingtontx.granicus.com/" +
    "ViewPublisherRSS.php?view_id=2&mode=vpodcast";
  let captured;
  const response = await handleRequest(request(metadata), ENV, async (url) => {
    captured = url;
    return new Response("<rss/>", { status: 200, headers: { "content-type": "text/xml" } });
  });
  assert.equal(
    captured,
    "https://arlingtontx.granicus.com/ViewPublisherRSS.php?view_id=2&mode=vpodcast",
  );
  assert.equal(response.status, 200);

  const download =
    "https://proxy.example/v1/granicus/arlingtontx.granicus.com/" +
    "DownloadFile.php?view_id=2&clip_id=3";
  const redirected = await handleRequest(request(download), ENV, async () =>
    new Response(null, { status: 302, headers: { location: "https://media.example/signed" } }),
  );
  assert.equal(redirected.status, 302);
  assert.equal(redirected.headers.get("location"), "https://media.example/signed");
});

test("rejects arbitrary Granicus hosts, paths, and query keys", async () => {
  const urls = [
    "https://proxy.example/v1/granicus/evil.example/Archive.php?view_id=2",
    "https://proxy.example/v1/granicus/arlingtontx.granicus.com/private?view_id=2",
    "https://proxy.example/v1/granicus/arlingtontx.granicus.com/Archive.php?token=secret",
  ];
  for (const url of urls) {
    const response = await handleRequest(request(url), ENV, async () => {
      throw new Error("must not fetch");
    });
    assert.equal(response.status, 404);
  }
});

test("HEAD omits the body and upstream redirects are refused", async () => {
  const head = await handleRequest(request(VALID_URL, { method: "HEAD" }), ENV, async () => {
    return new Response(null, {
      status: 200,
      headers: { "content-length": "123", "content-type": "video/mp4" },
    });
  });
  assert.equal(head.status, 200);
  assert.equal(head.headers.get("content-length"), "123");
  assert.equal(await head.text(), "");

  const redirect = await handleRequest(request(), ENV, async () => {
    return new Response(null, {
      status: 302,
      headers: { location: "https://example.com/private" },
    });
  });
  assert.equal(redirect.status, 502);
  assert.equal(redirect.headers.get("location"), null);
});

test("304 Not Modified passes through instead of being refused as a redirect", async () => {
  const response = await handleRequest(
    request(VALID_URL, { headers: { "if-none-match": '"etag-value"' } }),
    ENV,
    async () => {
      return new Response(null, {
        status: 304,
        headers: { etag: '"etag-value"' },
      });
    },
  );
  assert.equal(response.status, 304);
  assert.equal(response.headers.get("etag"), '"etag-value"');
  assert.equal(await response.text(), "");
});
