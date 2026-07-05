package gpuha

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"os"
	"sync"
	"time"

	"github.com/miekg/dns"
	"github.com/coredns/coredns/plugin"
)

type TargetNode struct {
	TTFT           int     `json:"ttft_ms"`
	VRAMSaturation float64 `json:"vram_saturation_pct"`
	SpotPrice      float64 `json:"spot_price_hr"`
	LastSeen       int64   `json:"last_seen_epoch"`
	SourceIP       string  `json:"source_ip"`
}

type GpuHaPlugin struct {
	Next          plugin.Handler
	StateCache    map[string]TargetNode
	Mu            sync.RWMutex
	LastBestIP    string
	DebounceTimer *time.Timer
	PendingIP     string
}

func NewGpuHaPlugin() *GpuHaPlugin {
	p := &GpuHaPlugin{
		StateCache: make(map[string]TargetNode),
	}
	go p.startStateListener()
	return p
}

func (p *GpuHaPlugin) Name() string { return "gpuha" }

func (p *GpuHaPlugin) startStateListener() {
	addr := net.UDPAddr{
		Port: 5006,
		IP:   net.ParseIP("0.0.0.0"),
	}
	conn, err := net.ListenUDP("udp", &addr)
	if err != nil {
		fmt.Printf("[GPUHA ERROR] Port 5006 bind failure: %v\n", err)
		return
	}
	defer conn.Close()

	fmt.Println("[GPUHA INIT] Edge State Broadcast Listener online on UDP port 5006.")

	buf := make([]byte, 2048)
	for {
		n, remoteAddr, err := conn.ReadFrom(buf)
		if err != nil {
			continue
		}

		fmt.Printf("[GPUHA WIRE] Received %d bytes from aggregator at %s\n", n, remoteAddr.String())

		var temporaryState map[string]TargetNode
		parseErr := json.Unmarshal(buf[:n], &temporaryState)
		if parseErr != nil {
			continue
		}

		p.Mu.Lock()
		for k, v := range temporaryState {
			p.StateCache[k] = v
		}

		bestIP := p.calculateBestRoute()

		if bestIP != "" && bestIP != p.LastBestIP && bestIP != p.PendingIP {
			fmt.Printf("[GPUHA CORE] Routing shift detected. Target %s is queued for commit...\n", bestIP)
			p.PendingIP = bestIP

			if p.DebounceTimer != nil {
				p.DebounceTimer.Stop()
			}

			p.DebounceTimer = time.AfterFunc(2500*time.Millisecond, func() {
				p.Mu.Lock()
				finalIP := p.PendingIP
				p.LastBestIP = finalIP
				p.PendingIP = ""
				p.Mu.Unlock()

				p.rewriteZoneFile(finalIP)
				p.sendDnsNotify()
			})
		}
		p.Mu.Unlock()
	}
}

func (p *GpuHaPlugin) ServeDNS(ctx context.Context, w dns.ResponseWriter, r *dns.Msg) (int, error) {
	return p.Next.ServeDNS(ctx, w, r)
}

func (p *GpuHaPlugin) calculateBestRoute() string {
	var fallbackNode string
	var fallbackTTFT = 999999
	var primaryNode string
	var primaryTTFT = 999999

	now := time.Now().Unix()

	for _, node := range p.StateCache {
		if now - node.LastSeen > 10 {
			continue
		}

		if node.VRAMSaturation <= 85.0 && node.SpotPrice <= 2.00 {
			if node.TTFT < primaryTTFT {
				primaryTTFT = node.TTFT
				primaryNode = node.SourceIP
			}
		} else {
			if node.TTFT < fallbackTTFT {
				fallbackTTFT = node.TTFT
				fallbackNode = node.SourceIP
			}
		}
	}

	if primaryNode != "" {
		return primaryNode
	}
	return fallbackNode
}

func (p *GpuHaPlugin) rewriteZoneFile(ip string) {
	serial := time.Now().Unix()
	zoneContent := fmt.Sprintf("$TTL 0\n@ IN SOA ns1.gpuha.com. admin.gpuha.com. (\n %d ; Serial\n 300 ; Refresh\n 60 ; Retry\n 1209600 ; Expire\n 0 ; Negative Cache TTL\n)\n@ IN NS ns1.gpuha.com.\nns1 IN A 127.0.0.1\n@ IN A %s\n", serial, ip)

	err := os.WriteFile("/root/coredns/api.gpuha.com.zone", []byte(zoneContent), 0644)
	if err != nil {
		fmt.Printf("[GPUHA ERROR] Zone write failure: %v\n", err)
	} else {
		fmt.Printf("[HIDDEN MASTER] State change committed and debounced. Serial %d assigned to path %s\n", serial, ip)
	}
}

func (p *GpuHaPlugin) sendDnsNotify() {
	targets := []string{
		"104.237.137.10:53",
		"45.79.109.10:53",
		"74.207.225.10:53",
		"143.42.7.10:53",
		"109.74.194.10:53",
	}

	m := new(dns.Msg)
	m.SetNotify("api.gpuha.com.")
	m.Authoritative = true

	c := new(dns.Client)
	c.Net = "udp"
	c.Timeout = 2 * time.Second

	for _, target := range targets {
		go func(t string) {
			_, _, err := c.Exchange(m, t)
			if err != nil {
				fmt.Printf("[NOTIFY] Outbound notify packet drop at %s: %v\n", t, err)
			} else {
				fmt.Printf("[NOTIFY] Handshake success! Outbound RFC-1996 accepted by -> %s\n", t)
			}
		}(target)
	}
}
