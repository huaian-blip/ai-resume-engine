/* GitHub Pages 部署配置
 * __API_BASE__ : 后端公网地址（cloudflared 隧道 URL，重启隧道后地址会变，需同步更新）
 * 安全说明：不再内置 __API_TOKEN__（公开站点的令牌等于透明）。
 * 浏览器请求由后端按 Origin 白名单（ALLOWED_ORIGINS）放行；
 * 非浏览器客户端请直接带 Authorization: Bearer <API_TOKEN> 调用后端。
 */
window.__API_BASE__ = 'https://subjects-copied-contacting-doll.trycloudflare.com';