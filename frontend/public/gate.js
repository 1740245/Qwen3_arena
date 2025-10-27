const form = document.getElementById('gate-form');
const input = document.getElementById('gate-input');
const button = document.getElementById('gate-button');
const toast = document.getElementById('gate-toast');

const showToast = (message) => {
  toast.textContent = message;
  toast.hidden = false;
  toast.classList.add('show');
  window.clearTimeout(showToast._timer);
  showToast._timer = window.setTimeout(() => {
    toast.classList.remove('show');
  }, 2500);
};

const setButtonState = (disabled) => {
  button.disabled = disabled;
};

const checkSession = async () => {
  try {
    const response = await fetch('/api/atlas/roster', {
      method: 'GET',
      credentials: 'include',
      redirect: 'follow',
    });
    if (response.ok && !response.redirected) {
      window.location.replace('/playground/');
    }
  } catch (error) {
    console.warn('Gate session check failed', error);
  }
};

const handleSubmit = async (event) => {
  event.preventDefault();
  const phrase = input.value.trim();
  if (!phrase) {
    showToast('ACCESS DENIED');
    return;
  }

  setButtonState(true);
  try {
    const response = await fetch('/api/session/login', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
      },
      credentials: 'include',
      body: JSON.stringify({ name: phrase }),
    });

    if (response.ok) {
      window.location.replace('/playground/');
      return;
    }
  } catch (error) {
    console.error('Gate login failed', error);
  } finally {
    setButtonState(false);
  }

  showToast('ACCESS DENIED');
};

input.addEventListener('input', () => {
  button.disabled = input.value.trim().length === 0;
});

form.addEventListener('submit', handleSubmit);

window.addEventListener('DOMContentLoaded', () => {
  input.focus();
  button.disabled = input.value.trim().length === 0;
  checkSession();
});
