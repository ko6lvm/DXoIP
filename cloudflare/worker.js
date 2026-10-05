/**
 * Cloudflare Worker: UDP Hole Punching Matchmaker
 * 
 * Free-tier ready (100k requests/day).
 * Globally distributed HTTPS endpoint for pairing peers by Room Key (e.g. 1234).
 * 
 * Setup:
 * 1. In Cloudflare Dashboard -> Workers & Pages -> KV -> Create Namespace "ROOMS".
 * 2. In Worker Settings -> Variables -> KV Namespace Bindings:
 *    Variable name: ROOMS
 *    KV namespace: ROOMS
 * (Or deploy via `npx wrangler deploy`).
 */

// Fallback in-memory map if KV is not yet bound
const MEMORY_ROOMS = new Map();

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type",
      "Content-Type": "application/json"
    };

    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // Health check
    if (url.pathname === "/" || url.pathname === "/health") {
      return new Response(JSON.stringify({
        status: "ok",
        service: "udp-matchmaker",
        has_kv: Boolean(env.ROOMS)
      }), { headers: corsHeaders });
    }

    // Helper functions to read/write state (KV with in-memory fallback)
    async function getRoom(roomKey) {
      if (env.ROOMS) {
        return await env.ROOMS.get(roomKey, { type: "json" });
      }
      return MEMORY_ROOMS.get(roomKey) || null;
    }

    async function setRoom(roomKey, data) {
      if (env.ROOMS) {
        // Automatically expires after 60 seconds
        await env.ROOMS.put(roomKey, JSON.stringify(data), { expirationTtl: 60 });
      } else {
        MEMORY_ROOMS.set(roomKey, data);
      }
    }

    async function deleteRoom(roomKey) {
      if (env.ROOMS) {
        await env.ROOMS.delete(roomKey);
      } else {
        MEMORY_ROOMS.delete(roomKey);
      }
    }

    // --- /join endpoint ---
    if (url.pathname === "/join") {
      const room = url.searchParams.get("room");
      const wan = url.searchParams.get("wan");
      const lan = url.searchParams.get("lan");

      if (!room || !wan || !lan) {
        return new Response(JSON.stringify({ error: "Required params: room, wan, lan" }), {
          status: 400,
          headers: corsHeaders
        });
      }

      const roomKey = `room:${room}`;
      const existing = await getRoom(roomKey);
      const now = Date.now();

      // Check if existing room is stale:
      // - p1 waiting for more than 45s without p2
      // - matched room older than 15s
      const isStale = existing && (
        (!existing.p2 && (now - existing.p1.ts > 45000)) ||
        (existing.p2 && (now - (existing.p2.ts || existing.p1.ts) > 15000))
      );

      // Check if the same peer is restarting/re-joining
      const isSamePeerAsP1 = existing && (existing.p1.wan === wan && existing.p1.lan === lan);

      if (!existing || isStale || (!existing.p2 && isSamePeerAsP1)) {
        // Peer 1 joined (or refreshed): store and wait
        const roomData = {
          p1: { wan, lan, ts: now },
          p2: null
        };
        await setRoom(roomKey, roomData);
        return new Response(JSON.stringify({
          status: "waiting",
          room,
          role: "p1",
          message: "Waiting for peer to join room"
        }), { headers: corsHeaders });

      } else if (!existing.p2) {
        // Peer 2 joined: store and introduce immediately
        existing.p2 = { wan, lan, ts: now };
        await setRoom(roomKey, existing);
        return new Response(JSON.stringify({
          status: "matched",
          room,
          role: "p2",
          peer_wan: existing.p1.wan,
          peer_lan: existing.p1.lan
        }), { headers: corsHeaders });

      } else {
        return new Response(JSON.stringify({ error: "Room already matched or full" }), {
          status: 409,
          headers: corsHeaders
        });
      }
    }

    // --- /leave or /reset endpoint ---
    if (url.pathname === "/leave" || url.pathname === "/reset") {
      const room = url.searchParams.get("room");
      if (room) {
        await deleteRoom(`room:${room}`);
      }
      return new Response(JSON.stringify({ status: "cleared", room }), {
        headers: corsHeaders
      });
    }

    // --- /poll endpoint (called by Peer 1 while waiting) ---
    if (url.pathname === "/poll") {
      const room = url.searchParams.get("room");
      if (!room) {
        return new Response(JSON.stringify({ error: "Missing room param" }), {
          status: 400,
          headers: corsHeaders
        });
      }

      const roomKey = `room:${room}`;
      const existing = await getRoom(roomKey);

      if (!existing) {
        return new Response(JSON.stringify({
          status: "expired",
          error: "Room expired or not found"
        }), { status: 404, headers: corsHeaders });
      }

      if (existing.p2) {
        // Match found! Delete room entry
        await deleteRoom(roomKey);
        return new Response(JSON.stringify({
          status: "matched",
          room,
          role: "p1",
          peer_wan: existing.p2.wan,
          peer_lan: existing.p2.lan
        }), { headers: corsHeaders });
      }

      return new Response(JSON.stringify({
        status: "waiting",
        room,
        role: "p1"
      }), { headers: corsHeaders });
    }

    return new Response(JSON.stringify({ error: "Not found" }), {
      status: 404,
      headers: corsHeaders
    });
  }
};
