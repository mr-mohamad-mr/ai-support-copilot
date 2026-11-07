/* AI Support Copilot — API client (infrastructure layer).
   Single place that talks HTTP: token storage, auth headers, error
   mapping. Views never call fetch() directly. All JSON is parsed here;
   rendering layers only ever receive plain data objects. */

(function (global) {
  "use strict";

  var TOKEN_KEY = "copilot.token";

  /** Error carrying the HTTP status so views can react (401 → re-login). */
  function ApiError(status, message) {
    var err = new Error(message || "Request failed");
    err.name = "ApiError";
    err.status = status;
    return err;
  }

  function createApi(baseUrl) {
    var base = (baseUrl || "").replace(/\/$/, "");

    function token() {
      try { return global.localStorage.getItem(TOKEN_KEY); }
      catch (e) { return null; }
    }

    function setToken(value) {
      try {
        if (value) { global.localStorage.setItem(TOKEN_KEY, value); }
        else { global.localStorage.removeItem(TOKEN_KEY); }
      } catch (e) { /* private mode: token lives in memory only */ }
      api._memoryToken = value || null;
    }

    function getToken() {
      return token() || api._memoryToken || null;
    }

    function headers(hasBody) {
      var h = {};
      if (hasBody) { h["Content-Type"] = "application/json"; }
      var t = getToken();
      if (t) { h["Authorization"] = "Bearer " + t; }
      return h;
    }

    function request(path, options) {
      options = options || {};
      return global.fetch(base + path, {
        method: options.method || "GET",
        headers: headers(Boolean(options.body)),
        body: options.body ? JSON.stringify(options.body) : undefined,
      }).then(function (resp) {
        if (resp.status === 204) { return null; }
        return resp.json().catch(function () { return {}; }).then(function (data) {
          if (!resp.ok) {
            var detail = data && data.detail;
            var message = typeof detail === "string" ? detail : "Request failed (" + resp.status + ")";
            if (resp.status === 401) { setToken(null); }
            throw ApiError(resp.status, message);
          }
          return data;
        });
      });
    }

    var api = {
      _memoryToken: null,
      isLoggedIn: function () { return Boolean(getToken()); },
      setToken: setToken,
      logout: function () { setToken(null); },

      register: function (tenantName, username, password) {
        return request("/auth/register", {
          method: "POST",
          body: { tenant_name: tenantName, username: username, password: password },
        });
      },
      login: function (username, password) {
        return request("/auth/login", {
          method: "POST",
          body: { username: username, password: password },
        }).then(function (data) {
          setToken(data.access_token);
          return data;
        });
      },
      me: function () { return request("/auth/me"); },

      listDocuments: function (page, pageSize) {
        return request("/documents?page=" + (page || 1) + "&page_size=" + (pageSize || 20));
      },
      getDocument: function (id) { return request("/documents/" + id); },
      createDocument: function (title, content, source) {
        return request("/documents", {
          method: "POST",
          body: { title: title, content: content, source: source || "manual" },
        });
      },
      deleteDocument: function (id) {
        return request("/documents/" + id, { method: "DELETE" });
      },

      chat: function (question, conversationId, topK) {
        var body = { question: question };
        if (conversationId) { body.conversation_id = conversationId; }
        if (topK) { body.top_k = topK; }
        return request("/chat", { method: "POST", body: body });
      },
      listConversations: function (page, pageSize) {
        return request("/conversations?page=" + (page || 1) + "&page_size=" + (pageSize || 20));
      },
      getConversation: function (id) { return request("/conversations/" + id); },
      deleteConversation: function (id) {
        return request("/conversations/" + id, { method: "DELETE" });
      },

      getAnalytics: function (days) {
        return request("/analytics/summary?days=" + (days || 14));
      },
    };
    return api;
  }

  global.Copilot = global.Copilot || {};
  global.Copilot.createApi = createApi;
  global.Copilot.api = createApi(""); // same-origin by default
})(typeof window !== "undefined" ? window : globalThis);
