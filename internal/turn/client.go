package turn

import (
	"fmt"
	"log"
	"net"
	"time"

	"github.com/pion/turn/v5"
)

// TURNClient wraps a TURN allocation and provides relay functionality.
type TURNClient struct {
	client    *turn.Client
	relayConn net.PacketConn
	conn      net.Conn
}

// NewTURNClient connects to a TURN server and creates an allocation.
func NewTURNClient(serverAddr, username, password string, useTCP bool) (*TURNClient, error) {
	var conn net.Conn
	var err error

	if useTCP {
		conn, err = net.DialTimeout("tcp", serverAddr, 10*time.Second)
	} else {
		udpAddr, resolveErr := net.ResolveUDPAddr("udp", serverAddr)
		if resolveErr != nil {
			return nil, fmt.Errorf("resolve %s: %w", serverAddr, resolveErr)
		}
		conn, err = net.DialUDP("udp", nil, udpAddr)
	}
	if err != nil {
		return nil, fmt.Errorf("connect to TURN %s: %w", serverAddr, err)
	}

	cfg := &turn.ClientConfig{
		STUNServerAddr: serverAddr,
		TURNServerAddr: serverAddr,
		Conn:           turn.NewSTUNConn(conn),
		Username:       username,
		Password:       password,
	}

	client, err := turn.NewClient(cfg)
	if err != nil {
		conn.Close()
		return nil, fmt.Errorf("create TURN client: %w", err)
	}

	if err := client.Listen(); err != nil {
		client.Close()
		conn.Close()
		return nil, fmt.Errorf("TURN listen: %w", err)
	}

	relayConn, err := client.Allocate()
	if err != nil {
		client.Close()
		conn.Close()
		return nil, fmt.Errorf("TURN allocate: %w", err)
	}

	log.Printf("TURN relay: %s (via %s)", relayConn.LocalAddr(), serverAddr)

	return &TURNClient{
		client:    client,
		relayConn: relayConn,
		conn:      conn,
	}, nil
}

// RelayConn returns the allocated relay PacketConn.
func (tc *TURNClient) RelayConn() net.PacketConn {
	return tc.relayConn
}

// CreatePermission grants relay permission for the given peer.
func (tc *TURNClient) CreatePermission(peerAddr net.Addr) error {
	return tc.client.CreatePermission(peerAddr)
}

// WriteTo sends data through the TURN relay to the specified peer.
func (tc *TURNClient) WriteTo(data []byte, peerAddr net.Addr) (int, error) {
	return tc.relayConn.WriteTo(data, peerAddr)
}

// ReadFrom reads data relayed by the TURN server.
func (tc *TURNClient) ReadFrom(buf []byte) (int, net.Addr, error) {
	return tc.relayConn.ReadFrom(buf)
}

// Close shuts down the TURN client.
func (tc *TURNClient) Close() error {
	if tc.relayConn != nil {
		tc.relayConn.Close()
	}
	if tc.client != nil {
		tc.client.Close()
	}
	if tc.conn != nil {
		tc.conn.Close()
	}
	return nil
}
