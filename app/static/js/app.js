/* AI Support Copilot — app shell (application layer).
   Hash routing (#/login, #/chat, #/admin), auth forms, session restore
   and the single toast/live region. Views (chat.js, admin.js) own their
   own DOM; this file only decides which view is visible. */

(function (global) {
  "use strict";

  var Copilot = global.Copilot;
  var api = Copilot.api;

  var App = {
    currentRoute: null,

    mount: function () {
      this.toastEl = document.getElementById("toast");
      this.tenantEl = document.getElementById("tenant-name");
      this.logoutBtn = document.getElementById("logout-btn");
      this.nav = document.getElementById("main-nav");
      this.views = {
        login: document.getElementById("view-login"),
        chat: document.getElementById("view-chat"),
        admin: document.getElementById("view-admin"),
        analytics: document.getElementById("view-analytics"),
      };

      document.getElementById("login-form").addEventListener("submit", this.onLogin.bind(this));
      document.getElementById("register-form").addEventListener("submit", this.onRegister.bind(this));
      this.logoutBtn.addEventListener("click", this.onLogout.bind(this));
      document.querySelectorAll(".auth-tabs button").forEach(function (btn) {
        btn.addEventListener("click", function () { App.selectAuthTab(btn.dataset.tab); });
      });
      this.nav.addEventListener("click", function (event) {
        var btn = event.target.closest("button[data-route]");
        if (btn) { global.location.hash = "#/" + btn.dataset.route; }
      });
      global.addEventListener("hashchange", this.route.bind(this));

      Copilot.chat.mount();
      Copilot.admin.mount();
      Copilot.analytics.mount();
      this.route();
    },

    toast: function (message, kind) {
      this.toastEl.textContent = message;
      this.toastEl.className = kind || "";
    },

    selectAuthTab: function (tab) {
      document.querySelectorAll(".auth-tabs button").forEach(function (btn) {
        btn.setAttribute("aria-selected", btn.dataset.tab === tab ? "true" : "false");
      });
      document.getElementById("login-form").hidden = tab !== "login";
      document.getElementById("register-form").hidden = tab !== "register";
    },

    setNav: function (route) {
      this.nav.querySelectorAll("button[data-route]").forEach(function (btn) {
        if (btn.dataset.route === route) {
          btn.setAttribute("aria-current", "page");
        } else {
          btn.removeAttribute("aria-current");
        }
      });
    },

    showView: function (name) {
      Object.keys(this.views).forEach(function (key) {
        App.views[key].hidden = key !== name;
      });
      var authed = api.isLoggedIn();
      this.nav.hidden = !authed;
      this.logoutBtn.hidden = !authed;
      this.tenantEl.hidden = !authed;
      this.currentRoute = name;
      this.setNav(name);
    },

    requireLogin: function () {
      api.logout();
      this.tenantEl.textContent = "";
      global.location.hash = "#/login";
      this.route();
    },

    route: function () {
      var hash = global.location.hash.replace(/^#\/?/, "") || "chat";
      var authed = api.isLoggedIn();

      if (!authed && hash !== "login") {
        global.location.hash = "#/login";
        hash = "login";
      }
      if (authed && hash === "login") {
        global.location.hash = "#/chat";
        hash = "chat";
      }
      if (hash !== "login" && hash !== "chat" && hash !== "admin" && hash !== "analytics") { hash = "chat"; }

      this.showView(hash);
      if (hash === "chat") {
        this.ensureSession().then(function () { Copilot.chat.show(); });
      } else if (hash === "admin") {
        this.ensureSession().then(function () { Copilot.admin.show(); });
      } else if (hash === "analytics") {
        this.ensureSession().then(function () { Copilot.analytics.show(); });
      } else {
        this.selectAuthTab("login");
      }
    },

    /** Validates the stored token once per route change; a dead token
        drops the user back to login instead of failing every request. */
    ensureSession: function () {
      var self = this;
      return api.me().then(function (me) {
        self.tenantEl.textContent = me.tenant.name + " · " + me.username + " (" + me.role + ")";
      }).catch(function (err) {
        if (err.status === 401) { self.requireLogin(); }
        else { self.toast("Could not reach the server: " + err.message, "error"); }
      });
    },

    readForm: function (form, fields) {
      var values = {};
      fields.forEach(function (name) {
        values[name] = form.elements[name].value.trim();
      });
      return values;
    },

    onLogin: function (event) {
      event.preventDefault();
      var form = event.target;
      var values = this.readForm(form, ["username", "password"]);
      if (!values.username || !values.password) {
        this.toast("Username and password are required.", "error");
        return;
      }
      var self = this;
      api.login(values.username, values.password).then(function () {
        self.toast("Signed in.", "ok");
        form.reset();
        global.location.hash = "#/chat";
        self.route();
      }).catch(function (err) {
        self.toast("Sign-in failed: " + err.message, "error");
      });
    },

    onRegister: function (event) {
      event.preventDefault();
      var form = event.target;
      var values = this.readForm(form, ["tenant_name", "username", "password"]);
      if (!values.tenant_name || !values.username || values.password.length < 8) {
        this.toast("Company name and username are required; password needs at least 8 characters.", "error");
        return;
      }
      var self = this;
      api.register(values.tenant_name, values.username, values.password).then(function () {
        return api.login(values.username, values.password);
      }).then(function () {
        self.toast("Workspace created — signed in.", "ok");
        form.reset();
        global.location.hash = "#/chat";
        self.route();
      }).catch(function (err) {
        self.toast("Registration failed: " + err.message, "error");
      });
    },

    onLogout: function () {
      api.logout();
      this.tenantEl.textContent = "";
      this.toast("Signed out.", "ok");
      this.requireLogin();
    },
  };

  Copilot.app = App;

  document.addEventListener("DOMContentLoaded", function () {
    App.mount();
  });
})(typeof window !== "undefined" ? window : globalThis);
