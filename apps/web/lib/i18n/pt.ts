import type { Dictionary } from "./en";
import { core } from "./dicts/core";
import { home } from "./dicts/home";
import { clients } from "./dicts/clients";
import { agents } from "./dicts/agents";
import { channels } from "./dicts/channels";
import { social } from "./dicts/social";
import { settings } from "./dicts/settings";
import { playground } from "./dicts/playground";
import { portal } from "./dicts/portal";
import { inbox } from "./dicts/inbox";
import { tools } from "./dicts/tools";
import { reports } from "./dicts/reports";

// Portuguese (Brazil) dictionary. Composed from the same modules; typed as Dictionary so it
// must mirror the exact shape of `en` (each area's `pt` mirrors its own `en`).
export const pt: Dictionary = {
  ...core.pt,
  home: home.pt,
  clients: clients.pt,
  agents: agents.pt,
  channels: channels.pt,
  social: social.pt,
  settings: settings.pt,
  playground: playground.pt,
  portal: portal.pt,
  inbox: inbox.pt,
  tools: tools.pt,
  reports: reports.pt,
};
