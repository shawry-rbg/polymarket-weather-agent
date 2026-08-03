"""Multi-agent subpackage for Polybot.

Agents:
  - live_temp_agent  : Free live temperature via Open-Meteo
  - rebalancer_agent : Intraday rebalancing based on live vs forecast deviation
  - orchestrator     : Multi-agent cycle coordinator (called by Modal cron)
"""
