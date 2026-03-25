package main

import (
	"crypto/rand"
	"encoding/hex"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"sync"
	"syscall"

	tunneldtls "github.com/kobzevvv/vk-calls-tunnel/internal/dtls"
	"github.com/kobzevvv/vk-calls-tunnel/internal/turn"
)

func main() {
	peer := flag.String("peer", "", "Server address (ip:port) — where TURN relays packets to")
	vkLink := flag.String("vk-link", "", "VK call link for TURN credentials")
	turnAddr := flag.String("turn", "", "Manual TURN server address (ip:port)")
	turnUser := flag.String("turn-user", "", "TURN username (for manual mode)")
	turnPass := flag.String("turn-pass", "", "TURN password (for manual mode)")
	listen := flag.String("listen", "127.0.0.1:9000", "Local UDP address for WireGuard")
	streams := flag.Int("n", 1, "Number of parallel TURN streams")
	useTCP := flag.Bool("tcp", true, "Use TCP for TURN transport")
	psk := flag.String("psk", "", "Pre-shared key (hex) for DTLS")
	sessionID := flag.String("session-id", "", "Fixed session ID (32-char hex)")
	noDTLS := flag.Bool("no-dtls", false, "Disable DTLS (may get banned by TURN provider)")
	flag.Parse()

	if *peer == "" {
		log.Fatal("-peer is required")
	}

	// Session UUID
	uuid := make([]byte, 16)
	if *sessionID != "" {
		var err error
		uuid, err = hex.DecodeString(*sessionID)
		if err != nil || len(uuid) != 16 {
			log.Fatal("-session-id must be 32-char hex")
		}
	} else {
		rand.Read(uuid)
	}
	log.Printf("Session: %x", uuid)

	// DTLS config
	dtlsCfg := tunneldtls.DefaultConfig()
	if *psk != "" {
		pskBytes, err := hex.DecodeString(*psk)
		if err != nil {
			log.Fatalf("Bad PSK: %v", err)
		}
		dtlsCfg.PSK = pskBytes
		dtlsCfg.PSKIdentity = "vk-tunnel"
	}

	// Get TURN credentials
	var creds *turn.Credentials
	if *vkLink != "" {
		var err error
		creds, err = turn.FetchFromLink(*vkLink)
		if err != nil {
			log.Fatalf("VK TURN credentials failed: %v", err)
		}
		log.Printf("Got %d TURN servers from VK", len(creds.TURNServers))
	} else if *turnAddr != "" {
		creds = turn.FetchFromManual(*turnAddr, *turnUser, *turnPass)
	} else {
		log.Fatal("Need -vk-link or -turn")
	}

	peerAddr, err := net.ResolveUDPAddr("udp", *peer)
	if err != nil {
		log.Fatalf("Bad peer address: %v", err)
	}

	// Create TURN streams
	type stream struct {
		turnClient *turn.TURNClient
		conn       net.Conn // DTLS conn (implements net.Conn) or raw relay
	}
	var activeStreams []stream
	var streamsMu sync.Mutex

	for i := 0; i < *streams; i++ {
		turnServer := creds.TURNServers[i%len(creds.TURNServers)]
		tc, err := turn.NewTURNClient(turnServer, creds.Username, creds.Password, *useTCP)
		if err != nil {
			log.Printf("Stream %d TURN failed: %v", i, err)
			continue
		}

		if err := tc.CreatePermission(peerAddr); err != nil {
			log.Printf("Stream %d permission failed: %v", i, err)
			tc.Close()
			continue
		}

		var conn net.Conn
		if !*noDTLS {
			conn, err = tunneldtls.Client(tc.RelayConn(), peerAddr, dtlsCfg)
			if err != nil {
				log.Printf("Stream %d DTLS failed: %v", i, err)
				tc.Close()
				continue
			}
		}

		activeStreams = append(activeStreams, stream{turnClient: tc, conn: conn})
		log.Printf("Stream %d/%d ready (via %s)", i+1, *streams, turnServer)
	}

	if len(activeStreams) == 0 {
		log.Fatal("No streams established")
	}

	// Local WireGuard listener
	listenAddr, err := net.ResolveUDPAddr("udp", *listen)
	if err != nil {
		log.Fatalf("Bad listen addr: %v", err)
	}
	wgConn, err := net.ListenUDP("udp", listenAddr)
	if err != nil {
		log.Fatalf("Listen: %v", err)
	}
	log.Printf("WireGuard listener on %s", *listen)

	// Shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("Shutting down...")
		wgConn.Close()
		for _, s := range activeStreams {
			if s.conn != nil {
				s.conn.Close()
			}
			s.turnClient.Close()
		}
		os.Exit(0)
	}()

	// Tunnel streams → WireGuard
	var lastWGClient *net.UDPAddr
	var clientMu sync.RWMutex

	for _, s := range activeStreams {
		if s.conn != nil {
			// DTLS mode: read from DTLS conn
			go func(c net.Conn) {
				buf := make([]byte, 1500)
				for {
					n, err := c.Read(buf)
					if err != nil {
						log.Printf("DTLS read: %v", err)
						return
					}
					clientMu.RLock()
					addr := lastWGClient
					clientMu.RUnlock()
					if addr != nil {
						wgConn.WriteToUDP(buf[:n], addr)
					}
				}
			}(s.conn)
		} else {
			// No-DTLS mode: read from TURN relay directly
			go func(tc *turn.TURNClient) {
				buf := make([]byte, 1500)
				for {
					n, _, err := tc.ReadFrom(buf)
					if err != nil {
						log.Printf("TURN read: %v", err)
						return
					}
					// Strip 16-byte UUID prefix from server responses
					if n <= 16 {
						continue
					}
					clientMu.RLock()
					addr := lastWGClient
					clientMu.RUnlock()
					if addr != nil {
						wgConn.WriteToUDP(buf[16:n], addr)
					}
				}
			}(s.turnClient)
		}
	}

	// WireGuard → tunnel (round-robin)
	var rrIndex uint64
	buf := make([]byte, 1500)
	for {
		n, addr, err := wgConn.ReadFromUDP(buf)
		if err != nil {
			log.Printf("WG read: %v", err)
			continue
		}

		clientMu.Lock()
		lastWGClient = addr
		clientMu.Unlock()

		// Prepend session UUID
		packet := make([]byte, 16+n)
		copy(packet[:16], uuid)
		copy(packet[16:], buf[:n])

		streamsMu.Lock()
		if len(activeStreams) > 0 {
			s := activeStreams[rrIndex%uint64(len(activeStreams))]
			rrIndex++
			if s.conn != nil {
				s.conn.Write(packet)
			} else {
				s.turnClient.WriteTo(packet, peerAddr)
			}
		}
		streamsMu.Unlock()
	}
}
