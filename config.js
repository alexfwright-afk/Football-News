/* RD Football News — the one file you edit after deploying the notification
   server. Both the app and the service worker read it, so the values only
   ever need typing once.

   Until pushServer is filled in, the app works exactly as it does now and the
   notification bells simply say notifications aren't set up yet. */

self.FN_CONFIG = {

  /* Your Cloudflare Worker address, with no trailing slash.
     Looks like: https://football-news-push.yourname.workers.dev
     Leave empty to turn notifications off entirely. */
  pushServer: "",

  /* Your public key from vapid-keys.txt. Safe to publish — it's designed to be. */
  vapidPublicKey: "BBBeiAVvFyt7yRkg-wkh5UR6BvNYtxF5nOyXUeJY5J-R8TKXUyQiN8lQ4tarsZ7aGcO92l1M_yD7rhdMmQkcG6M",
};
