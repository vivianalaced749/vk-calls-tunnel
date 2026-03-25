package turn

import (
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"regexp"
	"strings"
)

const (
	// VK Calls app credentials (public, embedded in VK web client)
	vkClientID     = "6287487"
	vkClientSecret = "QbYic1K3lEV5kTGiqlq2"
	// OK.ru SDK application key
	okAppKey = "CGMMEJLGDIHBABABA"
)

// Credentials holds TURN server authentication data.
type Credentials struct {
	Username    string
	Password    string
	TURNServers []string // IP:port list
}

var callIDRegexp = regexp.MustCompile(`/call/join/([A-Za-z0-9_-]+)`)

// FetchFromLink extracts TURN credentials from a VK call link.
// This is a 6-step anonymous OAuth chain:
// 1. Get anonymous token from login.vk.ru
// 2. Get anonymous access token payload from VK API
// 3. Get messages-scoped anonymous token
// 4. Get call anonymous token from VK API
// 5. Anonymous login to OK.ru calls backend
// 6. Join call via OK.ru to get TURN server config
func FetchFromLink(vkLink string) (*Credentials, error) {
	// Step 1: Get anonymous token
	anonToken, err := getAnonToken("")
	if err != nil {
		return nil, fmt.Errorf("step 1 (anon token): %w", err)
	}

	// Step 2: Get anonymous access token payload
	payload, err := getAnonAccessTokenPayload(anonToken, vkLink)
	if err != nil {
		return nil, fmt.Errorf("step 2 (access token payload): %w", err)
	}

	// Step 3: Get messages-scoped anonymous token
	msgToken, err := getAnonTokenMessages(payload)
	if err != nil {
		return nil, fmt.Errorf("step 3 (messages token): %w", err)
	}

	// Step 4: Get call anonymous token
	callToken, err := getCallAnonToken(msgToken, vkLink)
	if err != nil {
		return nil, fmt.Errorf("step 4 (call token): %w", err)
	}

	// Step 5: OK.ru anonymous login
	okSession, err := okAnonymousLogin(callToken)
	if err != nil {
		return nil, fmt.Errorf("step 5 (ok.ru login): %w", err)
	}

	// Step 6: Join call via OK.ru — get TURN credentials
	creds, err := joinCallOK(okSession, callToken, vkLink)
	if err != nil {
		return nil, fmt.Errorf("step 6 (join call): %w", err)
	}

	return creds, nil
}

// FetchFromManual creates credentials for a manually specified TURN server.
func FetchFromManual(turnAddr string, username, password string) *Credentials {
	return &Credentials{
		Username:    username,
		Password:    password,
		TURNServers: []string{turnAddr},
	}
}

// Step 1: Get anonymous token from login.vk.ru
func getAnonToken(tokenType string) (string, error) {
	params := url.Values{
		"client_id":     {vkClientID},
		"client_secret": {vkClientSecret},
	}
	if tokenType != "" {
		params.Set("token_type", tokenType)
	}

	resp, err := http.PostForm("https://login.vk.ru/?act=get_anonym_token", params)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var result struct {
		Token string `json:"token"`
	}
	if err := decodeJSON(resp.Body, &result); err != nil {
		return "", err
	}
	if result.Token == "" {
		return "", fmt.Errorf("empty anonymous token")
	}
	return result.Token, nil
}

// Step 2: Get anonymous access token payload
func getAnonAccessTokenPayload(anonToken, vkLink string) (string, error) {
	params := url.Values{
		"access_token": {anonToken},
		"join_link":    {vkLink},
		"v":            {"5.199"},
	}

	resp, err := http.PostForm("https://api.vk.com/method/calls.getAnonymousAccessTokenPayload", params)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var result struct {
		Response struct {
			Payload string `json:"payload"`
		} `json:"response"`
		Error *vkError `json:"error"`
	}
	if err := decodeJSON(resp.Body, &result); err != nil {
		return "", err
	}
	if result.Error != nil {
		return "", fmt.Errorf("VK API: %s", result.Error.Message)
	}
	return result.Response.Payload, nil
}

// Step 3: Get messages-scoped anonymous token
func getAnonTokenMessages(payload string) (string, error) {
	params := url.Values{
		"client_id":     {vkClientID},
		"client_secret": {vkClientSecret},
		"token_type":    {"messages"},
		"payload":       {payload},
	}

	resp, err := http.PostForm("https://login.vk.ru/?act=get_anonym_token", params)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var result struct {
		Token string `json:"token"`
	}
	if err := decodeJSON(resp.Body, &result); err != nil {
		return "", err
	}
	if result.Token == "" {
		return "", fmt.Errorf("empty messages token")
	}
	return result.Token, nil
}

// Step 4: Get call anonymous token
func getCallAnonToken(msgToken, vkLink string) (string, error) {
	params := url.Values{
		"access_token": {msgToken},
		"join_link":    {vkLink},
		"v":            {"5.199"},
	}

	resp, err := http.PostForm("https://api.vk.com/method/calls.getAnonymousToken", params)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var result struct {
		Response struct {
			Token string `json:"token"`
		} `json:"response"`
		Error *vkError `json:"error"`
	}
	if err := decodeJSON(resp.Body, &result); err != nil {
		return "", err
	}
	if result.Error != nil {
		return "", fmt.Errorf("VK API: %s", result.Error.Message)
	}
	return result.Response.Token, nil
}

// Step 5: OK.ru anonymous login
func okAnonymousLogin(callToken string) (string, error) {
	params := url.Values{
		"method":          {"auth.anonymLogin"},
		"application_key": {okAppKey},
		"token":           {callToken},
	}

	resp, err := http.PostForm("https://calls.okcdn.ru/fb.do", params)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()

	var result struct {
		SessionKey string `json:"session_key"`
	}
	if err := decodeJSON(resp.Body, &result); err != nil {
		return "", err
	}
	if result.SessionKey == "" {
		return "", fmt.Errorf("empty OK.ru session key")
	}
	return result.SessionKey, nil
}

// Step 6: Join call to get TURN credentials
func joinCallOK(sessionKey, callToken, vkLink string) (*Credentials, error) {
	// Extract the call hash from the link
	matches := callIDRegexp.FindStringSubmatch(vkLink)
	if len(matches) < 2 {
		return nil, fmt.Errorf("cannot extract call ID from: %s", vkLink)
	}
	callHash := matches[1]

	params := url.Values{
		"method":          {"vchat.joinConversationByLink"},
		"application_key": {okAppKey},
		"session_key":     {sessionKey},
		"call_hash":       {callHash},
	}

	resp, err := http.PostForm("https://calls.okcdn.ru/fb.do", params)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	body, err := io.ReadAll(resp.Body)
	if err != nil {
		return nil, err
	}

	// Parse the response — contains ICE servers with TURN credentials
	var result struct {
		ICEServers []struct {
			URLs       interface{} `json:"urls"` // can be string or []string
			Username   string      `json:"username"`
			Credential string      `json:"credential"`
		} `json:"ice_servers"`
		TURNServers []struct {
			URLs       interface{} `json:"urls"`
			Username   string      `json:"username"`
			Credential string      `json:"credential"`
		} `json:"turn_list"`
		Error *struct {
			Message string `json:"error_msg"`
		} `json:"error"`
	}

	if err := json.Unmarshal(body, &result); err != nil {
		return nil, fmt.Errorf("parse join response: %w (body: %s)", err, string(body))
	}

	if result.Error != nil {
		return nil, fmt.Errorf("OK.ru: %s", result.Error.Message)
	}

	creds := &Credentials{}

	// Collect from both ice_servers and turn_list
	type iceEntry struct {
		URLs       interface{}
		Username   string
		Credential string
	}
	var all []iceEntry
	for _, s := range result.ICEServers {
		all = append(all, iceEntry{s.URLs, s.Username, s.Credential})
	}
	for _, s := range result.TURNServers {
		all = append(all, iceEntry{s.URLs, s.Username, s.Credential})
	}

	for _, srv := range all {
		if creds.Username == "" && srv.Username != "" {
			creds.Username = srv.Username
			creds.Password = srv.Credential
		}
		for _, u := range extractURLs(srv.URLs) {
			addr := parseTURNURL(u)
			if addr != "" {
				creds.TURNServers = append(creds.TURNServers, addr)
			}
		}
	}

	if len(creds.TURNServers) == 0 {
		return nil, fmt.Errorf("no TURN servers in response: %s", string(body))
	}

	return creds, nil
}

// extractURLs handles both string and []string for URLs field.
func extractURLs(v interface{}) []string {
	switch urls := v.(type) {
	case string:
		return []string{urls}
	case []interface{}:
		var result []string
		for _, u := range urls {
			if s, ok := u.(string); ok {
				result = append(result, s)
			}
		}
		return result
	}
	return nil
}

// parseTURNURL converts "turn:1.2.3.4:3478?transport=tcp" to "1.2.3.4:3478"
func parseTURNURL(turnURL string) string {
	addr := turnURL
	addr = strings.TrimPrefix(addr, "turns:")
	addr = strings.TrimPrefix(addr, "turn:")

	if idx := strings.Index(addr, "?"); idx != -1 {
		addr = addr[:idx]
	}
	addr = strings.TrimSpace(addr)
	if addr == "" {
		return ""
	}
	if !strings.Contains(addr, ":") {
		addr += ":3478"
	}
	return addr
}

type vkError struct {
	Code    int    `json:"error_code"`
	Message string `json:"error_msg"`
}

func decodeJSON(r io.Reader, v interface{}) error {
	body, err := io.ReadAll(r)
	if err != nil {
		return err
	}
	return json.Unmarshal(body, v)
}
