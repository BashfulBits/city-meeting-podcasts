import assert from "node:assert/strict";
import test from "node:test";

import { handleRequest } from "../src/index.js";

const ENV = {
  ALLOWED_HOSTS:
    "austintx.new.swagit.com,dallastx.new.swagit.com,dentontx.new.swagit.com," +
    "addisontx.new.swagit.com,wacotx.new.swagit.com",
  PROXY_TOKEN: "test-secret",
};
const VALID_URL = "https://proxy.example/v1/swagit/austintx.new.swagit.com/views/117/city-council-meetings";

function request(url = VALID_URL, options = {}) {
  const headers = new Headers(options.headers);
  headers.set("authorization", "Bearer test-secret");
  return new Request(url, { ...options, headers });
}

test("requires bearer authentication before fetching upstream", async () => {
  let fetched = false;
  const response = await handleRequest(new Request(VALID_URL), ENV, async () => {
    fetched = true;
    return new Response();
  });
  assert.equal(response.status, 401);
  assert.equal(response.headers.get("www-authenticate"), "Bearer");
  assert.equal(fetched, false);
});

test("rejects a present-but-incorrect bearer token", async () => {
  let fetched = false;
  const response = await handleRequest(
    new Request(VALID_URL, { headers: { authorization: "Bearer wrong-secret" } }),
    ENV,
    async () => {
      fetched = true;
      return new Response();
    },
  );
  assert.equal(response.status, 401);
  assert.equal(fetched, false);
});

test("rejects unknown hosts, malformed paths, extra query params, and non-GET methods", async () => {
  const urls = [
    VALID_URL.replace("austintx.new.swagit.com", "evil.example"),
    VALID_URL.replace("austintx.new.swagit.com", "notallowed.new.swagit.com"),
    "https://proxy.example/v1/swagit/austintx.new.swagit.com/videos/0",
    "https://proxy.example/v1/swagit/austintx.new.swagit.com/videos/12345/agenda",
    "https://proxy.example/v1/swagit/austintx.new.swagit.com/views/117/a/b/c",
    `${VALID_URL}?page=1&token=secret`,
    `${VALID_URL}?redirect=https://example.com`,
  ];
  for (const url of urls) {
    const response = await handleRequest(request(url), ENV, async () => {
      throw new Error("must not fetch");
    });
    assert.equal(response.status, 404, url);
  }
  const post = await handleRequest(request(VALID_URL, { method: "POST" }), ENV);
  assert.equal(post.status, 405);
  assert.equal(post.headers.get("allow"), "GET");
});

test("fetches video pages and rejects query parameters on them", async () => {
  const videoUrl = "https://proxy.example/v1/swagit/austintx.new.swagit.com/videos/12345";
  let captured;
  const response = await handleRequest(request(videoUrl), ENV, async (url, init) => {
    captured = { url, init };
    return new Response("<html>chapters</html>", { status: 200 });
  });
  assert.equal(captured.url, "https://austintx.new.swagit.com/videos/12345");
  assert.equal(captured.init.redirect, "manual");
  assert.equal(response.status, 200);

  const refused = await handleRequest(request(`${videoUrl}?page=1`), ENV, async () => {
    throw new Error("must not fetch");
  });
  assert.equal(refused.status, 404);
});

test("fetches transcript endpoints", async () => {
  const transcriptUrl =
    "https://proxy.example/v1/swagit/austintx.new.swagit.com/videos/12345/transcript";
  let captured;
  const response = await handleRequest(request(transcriptUrl), ENV, async (url, init) => {
    captured = { url, init };
    return new Response("WEBVTT\n", { status: 200 });
  });
  assert.equal(captured.url, "https://austintx.new.swagit.com/videos/12345/transcript");
  assert.equal(captured.init.redirect, "manual");
  assert.equal(response.status, 200);
});

test("rejects a page value outside the bounded range", async () => {
  for (const page of ["0", "-1", "12345", "abc"]) {
    const response = await handleRequest(request(`${VALID_URL}?page=${page}`), ENV, async () => {
      throw new Error("must not fetch");
    });
    assert.equal(response.status, 404, page);
  }
});

test("fetches the upstream list page and forwards only the page query param", async () => {
  let captured;
  const response = await handleRequest(
    request(`${VALID_URL}?page=3`, { headers: { cookie: "must-not-forward" } }),
    ENV,
    async (url, init) => {
      captured = { url, init };
      return new Response("<html>page 3</html>", {
        status: 200,
        headers: {
          "content-type": "text/html; charset=utf-8",
          "content-length": "20",
          "set-cookie": "must-not-return",
        },
      });
    },
  );

  assert.equal(
    captured.url,
    "https://austintx.new.swagit.com/views/117/city-council-meetings?page=3",
  );
  assert.equal(captured.init.redirect, "manual");
  assert.equal(captured.init.headers.get("authorization"), null);
  assert.equal(captured.init.headers.get("cookie"), null);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("content-type"), "text/html; charset=utf-8");
  assert.equal(response.headers.get("cache-control"), "no-store");
  assert.equal(response.headers.get("set-cookie"), null);
  assert.equal(await response.text(), "<html>page 3</html>");
});

test("a bare (no page) request omits the query string upstream", async () => {
  let captured;
  await handleRequest(request(VALID_URL), ENV, async (url, init) => {
    captured = { url, init };
    return new Response("<html></html>", { status: 200 });
  });
  assert.equal(captured.url, "https://austintx.new.swagit.com/views/117/city-council-meetings");
});

test("upstream redirects to a disallowed host are refused", async () => {
  const response = await handleRequest(request(), ENV, async () => {
    return new Response(null, {
      status: 302,
      headers: { location: "https://example.com/private" },
    });
  });
  assert.equal(response.status, 502);
  assert.equal(response.headers.get("location"), null);
});

test("a same-tenant alias redirect (e.g. views/default/...) is followed once", async () => {
  let calls = 0;
  const response = await handleRequest(request(), ENV, async (url) => {
    calls += 1;
    if (calls === 1) {
      assert.equal(url, "https://austintx.new.swagit.com/views/117/city-council-meetings");
      return new Response(null, {
        status: 302,
        headers: { location: "https://austintx.new.swagit.com/views/117/city-council" },
      });
    }
    assert.equal(url, "https://austintx.new.swagit.com/views/117/city-council");
    return new Response("<html>resolved</html>", { status: 200 });
  });
  assert.equal(calls, 2);
  assert.equal(response.status, 200);
  assert.equal(await response.text(), "<html>resolved</html>");
});

test("a redirect to a disallowed host after the first hop is still refused", async () => {
  const response = await handleRequest(request(), ENV, async () => {
    return new Response(null, {
      status: 302,
      headers: { location: "https://evil.example/steal" },
    });
  });
  assert.equal(response.status, 502);
});

test("a redirect chain (two hops) is refused rather than followed indefinitely", async () => {
  let calls = 0;
  const response = await handleRequest(request(), ENV, async () => {
    calls += 1;
    return new Response(null, {
      status: 302,
      headers: {
        location: `https://austintx.new.swagit.com/views/117/hop-${calls}`,
      },
    });
  });
  assert.equal(calls, 2);
  assert.equal(response.status, 502);
});

test("a redirect to an unrecognized path shape is refused", async () => {
  const response = await handleRequest(request(), ENV, async () => {
    return new Response(null, {
      status: 302,
      headers: { location: "https://austintx.new.swagit.com/admin/secret" },
    });
  });
  assert.equal(response.status, 502);
});

test("download redirects are returned without following and expose only Location", async () => {
  const url = `${VALID_URL.replace("views/117/city-council-meetings", "videos/12345/download")}`;
  const response = await handleRequest(request(url), ENV, async () => {
    return new Response(null, {
      status: 302,
      headers: {
        location: "https://media.example/video.mp4?signature=secret",
        "set-cookie": "must-not-return",
      },
    });
  });
  assert.equal(response.status, 302);
  assert.equal(response.headers.get("location"), "https://media.example/video.mp4?signature=secret");
  assert.equal(response.headers.get("set-cookie"), null);
});

test("propagates a 403 from upstream so the caller can detect it", async () => {
  const response = await handleRequest(request(), ENV, async () => {
    return new Response("<html>forbidden</html>", { status: 403 });
  });
  assert.equal(response.status, 403);
});
