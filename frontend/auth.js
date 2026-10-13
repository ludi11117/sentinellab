// Keys are kept only for this tab session. The server derives project identity from the key.
(() => {
  const nativeFetch = window.fetch.bind(window);
  window.fetch = (resource, options = {}) => {
    const url = new URL(typeof resource === 'string' ? resource : resource.url, location.href);
    const token = sessionStorage.getItem('sentinel-project-key');
    if (token && url.origin === location.origin && url.pathname.startsWith('/api/public/')) {
      const headers = new Headers(options.headers || (resource instanceof Request ? resource.headers : {}));
      headers.set('Authorization', 'Bearer ' + token);
      return nativeFetch(resource, {...options, headers});
    }
    return nativeFetch(resource, options);
  };
  document.addEventListener('DOMContentLoaded', () => {
    const button = document.createElement('button');
    button.textContent = sessionStorage.getItem('sentinel-project-key') ? '切换项目' : '本地演示 / 连接项目';
    button.style.cssText = 'padding:6px 10px;border:1px solid #253248;border-radius:6px;background:#131c29;color:#92a2ba;cursor:pointer';
    button.onclick = () => {
      const key = prompt('粘贴项目访问密钥；留空切回本地演示（密钥仅保留在当前标签页）：');
      if (key === null) return;
      if (key.trim()) sessionStorage.setItem('sentinel-project-key', key.trim());
      else sessionStorage.removeItem('sentinel-project-key');
      location.reload();
    };
    document.querySelector('header')?.append(button);
  });
})();
