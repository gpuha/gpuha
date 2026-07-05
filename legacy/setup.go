package gpuha

import (
	"github.com/coredns/caddy"
	"github.com/coredns/coredns/core/dnsserver"
	"github.com/coredns/coredns/plugin"
)

func init() {
	caddy.RegisterPlugin("gpuha", caddy.Plugin{
		ServerType: "dns",
		Action:     setup,
	})
}

func setup(c *caddy.Controller) error {
	c.Next()
	gpuhaPlugin := NewGpuHaPlugin()
	dnsserver.GetConfig(c).AddPlugin(func(next plugin.Handler) plugin.Handler {
		gpuhaPlugin.Next = next
		return gpuhaPlugin
	})
	return nil
}
