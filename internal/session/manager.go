package session

import (
	"encoding/hex"
	"log"
	"net"
	"sync"
	"sync/atomic"
)

// Session represents a single client's tunnel session.
type Session struct {
	ID      string
	streams []net.Conn
	mu      sync.RWMutex
	wgConn  *net.UDPConn
	rrIndex atomic.Uint64
	closed  chan struct{}

	// Raw UDP return path (used in no-DTLS mode)
	udpConn    *net.UDPConn
	udpClients []net.Addr
	udpMu      sync.RWMutex
	udpRR      atomic.Uint64
}

// NewSession creates a tunnel session with a dedicated WireGuard connection.
func NewSession(id string, wgAddr *net.UDPAddr) (*Session, error) {
	wgConn, err := net.DialUDP("udp", nil, wgAddr)
	if err != nil {
		return nil, err
	}

	s := &Session{
		ID:     id,
		wgConn: wgConn,
		closed: make(chan struct{}),
	}

	go s.readFromWireGuard()
	return s, nil
}

// AddStream registers a DTLS connection for this session.
func (s *Session) AddStream(conn net.Conn) {
	s.mu.Lock()
	s.streams = append(s.streams, conn)
	count := len(s.streams)
	s.mu.Unlock()

	go s.readFromStream(conn)
	log.Printf("[%s] +stream (total: %d)", s.ID[:8], count)
}

// SetUDPReturn sets the UDP socket and client address for raw UDP responses.
func (s *Session) SetUDPReturn(conn *net.UDPConn, clientAddr net.Addr) {
	s.udpMu.Lock()
	s.udpConn = conn
	// Check if this client address is already known
	for _, a := range s.udpClients {
		if a.String() == clientAddr.String() {
			s.udpMu.Unlock()
			return
		}
	}
	s.udpClients = append(s.udpClients, clientAddr)
	s.udpMu.Unlock()
}

// HandlePacket processes a WireGuard packet received from the client.
func (s *Session) HandlePacket(data []byte) {
	if _, err := s.wgConn.Write(data); err != nil {
		log.Printf("[%s] wg write: %v", s.ID[:8], err)
	}
}

// sendToClient distributes data across streams (DTLS) or UDP clients round-robin.
func (s *Session) sendToClient(data []byte) {
	// Try DTLS streams first
	s.mu.RLock()
	n := len(s.streams)
	if n > 0 {
		idx := s.rrIndex.Add(1) % uint64(n)
		conn := s.streams[idx]
		s.mu.RUnlock()

		if _, err := conn.Write(data); err != nil {
			log.Printf("[%s] stream write: %v", s.ID[:8], err)
			s.removeStream(conn)
		}
		return
	}
	s.mu.RUnlock()

	// Fall back to raw UDP
	s.udpMu.RLock()
	nc := len(s.udpClients)
	if nc == 0 || s.udpConn == nil {
		s.udpMu.RUnlock()
		return
	}
	idx := s.udpRR.Add(1) % uint64(nc)
	clientAddr := s.udpClients[idx]
	conn := s.udpConn
	s.udpMu.RUnlock()

	// Prepend session UUID so client can identify responses
	sid, _ := hex.DecodeString(s.ID)
	packet := make([]byte, 16+len(data))
	copy(packet[:16], sid)
	copy(packet[16:], data)

	if _, err := conn.WriteTo(packet, clientAddr); err != nil {
		log.Printf("[%s] udp write to %s: %v", s.ID[:8], clientAddr, err)
	}
}

func (s *Session) readFromStream(conn net.Conn) {
	buf := make([]byte, 1500)
	for {
		select {
		case <-s.closed:
			return
		default:
		}

		n, err := conn.Read(buf)
		if err != nil {
			s.removeStream(conn)
			return
		}
		if n == 0 {
			continue
		}

		// Every packet from client has 16-byte session UUID prefix — strip it
		if n <= 16 {
			continue
		}
		s.HandlePacket(buf[16:n])
	}
}

func (s *Session) readFromWireGuard() {
	buf := make([]byte, 1500)
	for {
		select {
		case <-s.closed:
			return
		default:
		}

		n, err := s.wgConn.Read(buf)
		if err != nil {
			log.Printf("[%s] wg read: %v", s.ID[:8], err)
			return
		}
		if n == 0 {
			continue
		}

		s.sendToClient(buf[:n])
	}
}

func (s *Session) removeStream(conn net.Conn) {
	s.mu.Lock()
	defer s.mu.Unlock()
	for i, c := range s.streams {
		if c == conn {
			conn.Close()
			s.streams = append(s.streams[:i], s.streams[i+1:]...)
			log.Printf("[%s] -stream (remaining: %d)", s.ID[:8], len(s.streams))
			return
		}
	}
}

// Close terminates the session.
func (s *Session) Close() {
	close(s.closed)
	s.mu.Lock()
	for _, c := range s.streams {
		c.Close()
	}
	s.streams = nil
	s.mu.Unlock()
	s.wgConn.Close()
}

// Manager manages client sessions.
type Manager struct {
	sessions map[string]*Session
	mu       sync.RWMutex
	wgAddr   *net.UDPAddr
}

// NewManager creates a session manager.
func NewManager(wgAddr *net.UDPAddr) *Manager {
	return &Manager{
		sessions: make(map[string]*Session),
		wgAddr:   wgAddr,
	}
}

// GetOrCreate returns or creates a session.
func (m *Manager) GetOrCreate(sessionID string) (*Session, error) {
	m.mu.RLock()
	if s, ok := m.sessions[sessionID]; ok {
		m.mu.RUnlock()
		return s, nil
	}
	m.mu.RUnlock()

	m.mu.Lock()
	defer m.mu.Unlock()

	if s, ok := m.sessions[sessionID]; ok {
		return s, nil
	}

	s, err := NewSession(sessionID, m.wgAddr)
	if err != nil {
		return nil, err
	}
	m.sessions[sessionID] = s
	log.Printf("[manager] new session: %s", sessionID[:8])
	return s, nil
}
