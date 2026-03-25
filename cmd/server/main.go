package main

import (
	"encoding/hex"
	"flag"
	"log"
	"net"
	"os"
	"os/signal"
	"syscall"

	tunneldtls "github.com/kobzevvv/vk-calls-tunnel/internal/dtls"
	"github.com/kobzevvv/vk-calls-tunnel/internal/session"
)

func main() {
	listen := flag.String("listen", "0.0.0.0:56000", "UDP address to listen on")
	connect := flag.String("connect", "127.0.0.1:51820", "WireGuard backend address")
	psk := flag.String("psk", "", "Pre-shared key (hex) for DTLS")
	noDTLS := flag.Bool("no-dtls", false, "Disable DTLS, accept raw UDP")
	flag.Parse()

	wgAddr, err := net.ResolveUDPAddr("udp", *connect)
	if err != nil {
		log.Fatalf("Bad WireGuard address: %v", err)
	}

	mgr := session.NewManager(wgAddr)

	dtlsCfg := tunneldtls.DefaultConfig()
	if *psk != "" {
		pskBytes, err := hex.DecodeString(*psk)
		if err != nil {
			log.Fatalf("Bad PSK: %v", err)
		}
		dtlsCfg.PSK = pskBytes
		dtlsCfg.PSKIdentity = "vk-tunnel"
	}

	// Shutdown
	sigCh := make(chan os.Signal, 1)
	signal.Notify(sigCh, syscall.SIGINT, syscall.SIGTERM)
	go func() {
		<-sigCh
		log.Println("Shutting down...")
		os.Exit(0)
	}()

	if *noDTLS {
		runRawUDP(*listen, mgr)
	} else {
		runDTLS(*listen, mgr, dtlsCfg)
	}
}

func runDTLS(addr string, mgr *session.Manager, cfg *tunneldtls.Config) {
	ln, err := tunneldtls.Listen(addr, cfg)
	if err != nil {
		log.Fatalf("DTLS listen: %v", err)
	}
	log.Printf("Server (DTLS) listening on %s", addr)

	for {
		conn, err := ln.Accept()
		if err != nil {
			log.Printf("DTLS accept: %v", err)
			continue
		}
		go handleDTLSConn(conn, mgr)
	}
}

func handleDTLSConn(conn net.Conn, mgr *session.Manager) {
	remote := conn.RemoteAddr().String()

	// First read: session UUID (16 bytes)
	uuidBuf := make([]byte, 16)
	n, err := conn.Read(uuidBuf)
	if err != nil || n < 16 {
		log.Printf("UUID read from %s: %v (got %d bytes)", remote, err, n)
		conn.Close()
		return
	}

	sid := hex.EncodeToString(uuidBuf)
	log.Printf("DTLS stream from %s, session %s", remote, sid[:8])

	sess, err := mgr.GetOrCreate(sid)
	if err != nil {
		log.Printf("Session create: %v", err)
		conn.Close()
		return
	}
	sess.AddStream(conn)
}

func runRawUDP(addr string, mgr *session.Manager) {
	udpAddr, err := net.ResolveUDPAddr("udp", addr)
	if err != nil {
		log.Fatalf("Bad address: %v", err)
	}

	conn, err := net.ListenUDP("udp", udpAddr)
	if err != nil {
		log.Fatalf("UDP listen: %v", err)
	}
	log.Printf("Server (raw UDP) listening on %s", addr)

	buf := make([]byte, 1500)
	for {
		n, remoteAddr, err := conn.ReadFromUDP(buf)
		if err != nil {
			log.Printf("UDP read: %v", err)
			continue
		}

		// First 16 bytes are session UUID
		if n <= 16 {
			continue
		}

		sid := hex.EncodeToString(buf[:16])
		payload := make([]byte, n-16)
		copy(payload, buf[16:n])

		sess, err := mgr.GetOrCreate(sid)
		if err != nil {
			log.Printf("Session: %v", err)
			continue
		}

		// For raw UDP, we need to track the remote address to send responses back
		// This is handled differently — the session manager sends back via the same conn
		_ = remoteAddr
		sess.HandlePacket(payload)
	}
}
