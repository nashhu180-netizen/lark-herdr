# lark-herdr

A Linux-first bridge between Feishu/Lark and HerdR for managing multiple
workspaces and communicating with their orchestration agents.

## Status

Design and implementation are in progress.

## Goals

- Bind a Feishu chat or group to a HerdR workspace and orchestration agent.
- Communicate with agents already running inside HerdR panes.
- Create new HerdR workspaces and start supported agents from Feishu.
- Keep multiple concurrent task workspaces isolated and easy to manage.
- Run and deploy reliably on Linux.
