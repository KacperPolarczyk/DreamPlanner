import data from './database/fixtures/dreamplanner.json';

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL;
const cellStyle = { padding: '10px', borderBottom: '1px solid #ddd' };

export default function DataList() {
  const schedule = data.schedules.find((s) => s.id === 1);

  const shifts = data.shift_requirements.filter(
    (shift) => shift.schedule_id === schedule?.id
  );

  return (
    <div style={{ padding: '20px', fontFamily: 'sans-serif', maxWidth: 1000, margin: '0 auto' }}>
      <h2>
        Grafik DreamPlanner – {schedule?.month}/{schedule?.year} ({schedule?.status})
      </h2>

      <table style={{ width: '100%', borderCollapse: 'collapse', textAlign: 'left' }}>
        <thead>
          <tr style={{ borderBottom: '2px solid #999' }}>
            <th style={cellStyle}>Data</th>
            <th style={cellStyle}>Godziny</th>
            <th style={cellStyle}>Stanowisko</th>
            <th style={cellStyle}>Obsada</th>
            <th style={cellStyle}>Pracownicy</th>
            <th style={cellStyle}>Uwagi</th>
          </tr>
        </thead>
        <tbody>
          {shifts.map((shift) => {
            const position = data.positions.find((p) => p.id === shift.position_id);
            const assignments = data.shift_assignments.filter(
              (a) => a.shift_requirement_id === shift.id
            );
            const isFull = assignments.length >= shift.required_workers;

            return (
              <tr key={shift.id}>
                <td style={cellStyle}>{shift.work_date}</td>
                <td style={cellStyle}>
                  {shift.start_time.slice(0, 5)} – {shift.end_time.slice(0, 5)}
                </td>
                <td style={cellStyle}>{position?.name}</td>
                <td style={{ ...cellStyle, color: isFull ? 'green' : 'red' }}>
                  {assignments.length}/{shift.required_workers}
                </td>
                <td style={cellStyle}>
                  {assignments.map((a) => {
                    const user = data.users.find((u) => u.id === a.user_id);
                    return (
                      <div key={a.id}>
                        {user?.full_name}
                        {a.warning_type ? ' ⚠️' : ''}
                      </div>
                    );
                  })}
                </td>
                <td style={cellStyle}>{shift.note ?? '–'}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
      <p style={{ color: '#888', marginTop: 20 }}>API: {API_BASE_URL}</p>
    </div>
  );
}