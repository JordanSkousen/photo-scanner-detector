private readonly List<Holiday> _holidays = new List<Holiday>()
{
  new Holiday("New Year's", 1, 1),
  new Holiday("Martin Luther King Jr.", 1, 2, 3),
  new Holiday("Washington's Birthday", 2, 2, 3),
  new Holiday("Valentine's", 2, 14),
  new Holiday("St. Patrick's", 3, 17),
  new Holiday("Memorial Day", 5, 2, 4),
  new Holiday("Mother's Day", 5, 2, 2),
  new Holiday("Father's Day", 6, 1, 3),
  new Holiday("Independence Day", 7, 4),
  new Holiday("Labor Day", 9, 2, 1),
  new Holiday("Columbus Day", 10, 2, 2),
  new Holiday("Halloween", 10, 31),
  new Holiday("Veterans Day", 11, 11),
  new Holiday("Thanksgiving", 11, 5, 4),
  new Holiday("Christmas Eve", 12, 24),
  new Holiday("Christmas", 12, 25),
  new Holiday("New Year's Eve", 12, 31)
};

DateTime GetHoliday(string s, int year)
{
  s = RemovePunctuation(s).ToLower();
  foreach (var holi in _holidays)
  {
    if (s.Contains(RemovePunctuation(holi.Name.ToLower())))
    {
      return holi.GetDateTime(year);
    }
  }
  if (s.Contains("easter"))
  {
    //special easter crap, going to die
    var month = ((19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30 + (32 + 2 * (year / 100 % 4) + 2 * (year % 100 / 4) - (19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30 - year % 100 % 4) % 7 - 7 * ((year % 19 + 11 * ((19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30) + 22 * ((32 + 2 * (year / 100 % 4) + 2 * (year % 100 / 4) - (19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30 - year % 100 % 4) % 7)) / 451) + 114) / 31;
    var day = (((19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30 + (32 + 2 * (year / 100 % 4) + 2 * (year % 100 / 4) - (19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30 - year % 100 % 4) % 7 - 7 * ((year % 19 + 11 * ((19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30) + 22 * ((32 + 2 * (year / 100 % 4) + 2 * (year % 100 / 4) - (19 * (year % 19) + year / 100 - (year / 100) / 4 - (year / 100 - (year / 100 + 8) / 25 + 1) / 3 + 15) % 30 - year % 100 % 4) % 7)) / 451) + 114) % 31) + 1;
    return new DateTime(year, month, day);
  }

  return new DateTime(1, 1, 1);
}

string RemovePunctuation(string s)
{
  var regex = new System.Text.RegularExpressions.Regex("[.,?!'\":;@#$%^&*()]");
  return regex.Replace(s, "");
}

class Holiday
{
  public Holiday(string name, int month, int day)
  {
    Name = name;
    Month = month;
    Day = day;
    DayOfWeek = 0;
    Fixed = true;
  }
  public Holiday(string name, int month, int dayOfWeek, int weekOfMonth)
  {
    Name = name;
    Month = month;
    DayOfWeek = 0;
    DayOfWeek = dayOfWeek;
    WeekOfMonth = weekOfMonth;
    Fixed = false;
  }
  public DateTime GetDateTime(int year)
  {
    if (Fixed)
    {
      return new DateTime(year, Month, Day);
    }
    else
    {
      //var dt = new DynamicDT(new DateTime(year, Month, 1));
      int dayOfWeekPassed = 0;
      for (int i = 1; i <= DateTime.DaysInMonth(year, Month); i++)
      {
        var dt = new DateTime(year, Month, i);
        if ((int)dt.DayOfWeek + 1 == DayOfWeek)
        {
          dayOfWeekPassed++;
        }
        if (dayOfWeekPassed == WeekOfMonth && (int)dt.DayOfWeek + 1 == DayOfWeek)
        {
          return dt;
        }
      }
      return new DateTime();
    }
  }
  public string Name;
  public int Month;
  public int Day;
  public bool Fixed;
  public int DayOfWeek;
  public int WeekOfMonth;
}