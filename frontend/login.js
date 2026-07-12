// Login-Seite: schickt Benutzername/Passwort an POST /api/auth/login. Bei
// Erfolg setzt der Server ein httponly-Session-Cookie und wir leiten zum
// Dashboard weiter. War der Nutzer bereits eingeloggt (Cookie noch gueltig),
// leiten wir direkt weiter, ohne das Formular zu zeigen.

const el = (id) => document.getElementById(id);

async function redirectIfAlreadyLoggedIn() {
  try {
    const res = await fetch("/api/auth/me");
    if (res.ok) {
      window.location.href = "index.html";
    }
  } catch (err) {
    // Netzwerkfehler: einfach das Login-Formular anzeigen lassen.
    console.error(err);
  }
}

function setupLoginForm() {
  const form = el("login-form");
  const errorEl = el("login-error");
  const submitBtn = el("login-submit");

  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    errorEl.textContent = "";
    submitBtn.disabled = true;
    submitBtn.textContent = "Anmelden …";

    const username = el("username").value.trim();
    const password = el("password").value;

    try {
      const res = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username, password }),
      });

      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        errorEl.textContent = data.detail || "Anmeldung fehlgeschlagen.";
        return;
      }

      window.location.href = "index.html";
    } catch (err) {
      console.error(err);
      errorEl.textContent = "Verbindung zum Server fehlgeschlagen.";
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Anmelden";
    }
  });
}

redirectIfAlreadyLoggedIn();
setupLoginForm();
